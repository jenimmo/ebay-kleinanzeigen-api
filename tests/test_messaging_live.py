"""Explizit freigegebener Nachrichtentest ausschließlich eigener Accounts.

Der einzige Netzwerk-/Schreibtest hat den Marker ``live_messaging``. Der breite
Schalter ``--run-live`` aktiviert ihn nicht. Sicherheitsvoraussetzungen werden
vor dem ersten Upload und vor jedem Versand erneut geprüft. Dieses Modul startet
keinen Server, meldet keine Accounts an und erstellt keine Anzeigen.
"""

from __future__ import annotations

import math
import os
import re
import struct
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit
import zlib

import httpx
import pytest

from conftest import pytest_collection_modifyitems


def test_general_live_flag_never_enables_message_sending() -> None:
    """Even double-marked tests need the dedicated messaging switch."""
    for ordinary_live in (False, True):
        added = []
        config = SimpleNamespace(
            getoption=lambda name: ordinary_live if name == "--run-live" else False
        )
        item = SimpleNamespace(
            get_closest_marker=lambda name: (
                object() if name in {"live", "live_messaging"} else None
            ),
            add_marker=added.append,
        )
        pytest_collection_modifyitems(config, [item])
        assert len(added) == 1
        assert added[0].name == "skip"
        assert "--run-live-messaging" in added[0].kwargs["reason"]


def test_messaging_opt_in_is_independent_of_search_live_opt_in() -> None:
    added = []
    config = SimpleNamespace(getoption=lambda name: name == "--run-live-messaging")
    item = SimpleNamespace(
        get_closest_marker=lambda name: (
            object() if name in {"live", "live_messaging"} else None
        ),
        add_marker=added.append,
    )
    pytest_collection_modifyitems(config, [item])
    assert not added


def _required(name: str, pattern: str) -> str:
    value = os.environ.get(name, "")
    if not re.fullmatch(pattern, value):
        pytest.fail(f"{name} fehlt oder hat kein gültiges Format; kein Versand")
    return value


def _run_id() -> str:
    # Re-running pytest must not silently create fresh sending operations. Keep
    # this explicit identifier for the same authorised test, including retries.
    return _required(
        "KLEINANZEIGEN_LIVE_MESSAGING_RUN_ID", r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
    )


def test_live_run_requires_explicit_stable_identifier(monkeypatch) -> None:
    monkeypatch.delenv("KLEINANZEIGEN_LIVE_MESSAGING_RUN_ID", raising=False)
    with pytest.raises(pytest.fail.Exception, match="RUN_ID"):
        _run_id()
    monkeypatch.setenv("KLEINANZEIGEN_LIVE_MESSAGING_RUN_ID", "eigener-test-20261104")
    assert _run_id() == _run_id() == "eigener-test-20261104"
    monkeypatch.setenv("KLEINANZEIGEN_LIVE_MESSAGING_RUN_ID", "kein gültiger Schlüssel")
    with pytest.raises(pytest.fail.Exception, match="RUN_ID"):
        _run_id()


def _json(response: httpx.Response) -> dict[str, Any]:
    # Never put a private chat body, a cookie, or an authentication page into
    # pytest's failure output.
    if response.status_code not in {200, 201, 202}:
        pytest.fail(f"Nachrichtenabnahme: unerwartetes HTTP {response.status_code}")
    result = response.json()
    if not isinstance(result, dict):
        pytest.fail("Nachrichtenabnahme: unerwartetes Antwortformat")
    return result


def _timeout() -> float:
    value = float(os.environ.get("KLEINANZEIGEN_LIVE_POLL_TIMEOUT_SECONDS", "900"))
    if not math.isfinite(value) or value <= 0:
        pytest.fail("KLEINANZEIGEN_LIVE_POLL_TIMEOUT_SECONDS muss positiv sein")
    return value


def _require_policy(
    client: httpx.Client,
    account_a: str,
    account_b: str,
    listing_id: str,
) -> tuple[str, str]:
    response = _json(client.get("/messaging/accounts"))
    policy = response.get("test_policy", {})
    if policy.get("enabled") is not True:
        pytest.fail(
            "Serverseitiger Nachrichten-Testmodus ist nicht aktiv; kein Versand"
        )
    permitted_accounts = set(policy.get("account_ids", []))
    permitted_users = set(policy.get("user_ids", []))
    permitted_listings = set(policy.get("listing_ids", []))
    if account_a == account_b or not {account_a, account_b} <= permitted_accounts:
        pytest.fail("Zwei getrennte eigene Testaccounts sind nicht freigegeben")
    if listing_id not in permitted_listings:
        pytest.fail("Die eigene Testanzeige ist nicht serverseitig freigegeben")
    accounts = {item["account_id"]: item for item in response.get("accounts", [])}
    own_user_ids = []
    for account_id in (account_a, account_b):
        account = accounts.get(account_id, {})
        if account.get("state") != "connected":
            pytest.fail("Beide Testaccounts müssen vorher manuell angemeldet sein")
        user_id = account.get("user_id")
        if not user_id or user_id not in permitted_users:
            pytest.fail(
                "Verifizierte Nutzer-ID fehlt in der serverseitigen Testfreigabe"
            )
        own_user_ids.append(str(user_id))
    if own_user_ids[0] == own_user_ids[1]:
        pytest.fail("Die beiden Profile gehören demselben Account; kein Versand")
    return own_user_ids[0], own_user_ids[1]


@pytest.mark.parametrize(
    "case",
    [
        "disabled",
        "missing_account",
        "missing_user",
        "wrong_listing",
        "disconnected",
        "same_user",
    ],
)
def test_live_harness_rejects_unverified_test_scope(case: str) -> None:
    """A mistaken test configuration must fail before any write request."""
    payload = {
        "test_policy": {
            "enabled": True,
            "account_ids": ["own-a", "own-b"],
            "user_ids": ["123", "456"],
            "listing_ids": ["1234567890"],
        },
        "accounts": [
            {"account_id": "own-a", "user_id": "123", "state": "connected"},
            {"account_id": "own-b", "user_id": "456", "state": "connected"},
        ],
    }
    if case == "disabled":
        payload["test_policy"]["enabled"] = False
    elif case == "missing_account":
        payload["test_policy"]["account_ids"] = ["own-a"]
    elif case == "missing_user":
        payload["test_policy"]["user_ids"] = ["123"]
    elif case == "wrong_listing":
        payload["test_policy"]["listing_ids"] = ["9999999999"]
    elif case == "disconnected":
        payload["accounts"][1]["state"] = "reauth_required"
    else:
        payload["accounts"][1]["user_id"] = "123"
    methods = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json=payload)

    with httpx.Client(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(pytest.fail.Exception):
            _require_policy(client, "own-a", "own-b", "1234567890")
    assert methods == ["GET"]


def _verify_listing_owner(
    client: httpx.Client, listing_id: str, owner_user_id: str, run_id: str
) -> None:
    """Read the existing listing and fail before messaging if B is not its owner."""
    accepted = _json(
        client.post(
            "/jobs/details",
            json={"listing_ids": [listing_id]},
            headers={"Idempotency-Key": f"messaging-preflight-{run_id}"},
        )
    )
    deadline = time.monotonic() + _timeout()
    while time.monotonic() < deadline:
        job = _json(client.get(f"/jobs/{accepted['job_id']}"))
        if job.get("state") == "completed":
            results = _json(client.get(f"/jobs/{accepted['job_id']}/results"))
            items = results.get("items", [])
            seller_id = (
                ((items[0].get("data") or {}).get("seller") or {}).get("user_id")
                if len(items) == 1
                else None
            )
            if str(seller_id) != owner_user_id:
                pytest.fail(
                    "Eigentümer der Testanzeige ist nicht Account B; kein Versand"
                )
            return
        if job.get("state") in {"needs_attention", "cancelled"}:
            pytest.fail("Eigentümer der Testanzeige konnte nicht verifiziert werden")
        time.sleep(2)
    pytest.fail("Eigentümerprüfung überschritt das Zeitlimit; kein Versand")


def _test_png() -> bytes:
    """Create a harmless 16×16 green image without reading any local file."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    pixels = b"".join(b"\x00" + b"\x30\xa0\x60" * 16 for _ in range(16))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def _wait_for_send(
    client: httpx.Client, account_id: str, accepted: dict[str, Any]
) -> dict[str, Any]:
    deadline = time.monotonic() + _timeout()
    operation_id = accepted["operation_id"]
    while time.monotonic() < deadline:
        operation = _json(
            client.get(f"/messaging/accounts/{account_id}/operations/{operation_id}")
        )
        state = operation.get("state")
        if state == "sent":
            return operation
        if state in {
            "partial",
            "failed",
            "outcome_unknown",
            "reauth_required",
            "blocked",
        }:
            pytest.fail(
                f"Nachrichtenoperation endete mit {state}; nicht erneut senden, "
                "bestehende Operation prüfen"
            )
        time.sleep(2)
    pytest.fail("Versandstatus unklar nach Zeitlimit; bestehenden Auftrag prüfen")


def _find_conversation(
    client: httpx.Client, account_id: str, peer_user_id: str, listing_id: str
) -> str | None:
    cursor = None
    for _ in range(100):
        params: dict[str, Any] = {"source": "live", "limit": 50}
        if cursor:
            params["cursor"] = cursor
        page = _json(
            client.get(f"/messaging/accounts/{account_id}/conversations", params=params)
        )
        for conversation in page.get("conversations", []):
            remote_listing = str(conversation.get("listing_id", "")).split("-", 1)[0]
            if (
                str(conversation.get("recipient_user_id")) == peer_user_id
                and remote_listing == listing_id.split("-", 1)[0]
            ):
                return str(conversation["conversation_id"])
        if not page.get("has_more"):
            return None
        next_cursor = page.get("next_cursor")
        if not next_cursor or next_cursor == cursor:
            pytest.fail("Ungültige Gesprächspaginierung; kein weiterer Versand")
        cursor = next_cursor
    pytest.fail(
        "Gespräch konnte innerhalb der begrenzten Suche nicht zugeordnet werden"
    )


def _conversation(
    client: httpx.Client, account_id: str, conversation_id: str, source: str = "live"
) -> dict[str, Any]:
    return _json(
        client.get(
            f"/messaging/accounts/{account_id}/conversations/{conversation_id}",
            params={"source": source, "limit": 200},
        )
    )


def _same_message_text(actual: Any, expected: str) -> bool:
    # Kleinanzeigen preserves the text but returns HTML-form line endings as
    # CRLF. Do not accept trimming, truncation or other whitespace changes.
    return isinstance(actual, str) and actual.replace("\r\n", "\n") == expected.replace(
        "\r\n", "\n"
    )


def test_received_text_allows_only_form_line_ending_conversion() -> None:
    assert _same_message_text(
        "Grüße, Äpfel und Öl.\r\nZweite Zeile", "Grüße, Äpfel und Öl.\nZweite Zeile"
    )
    for changed in ("Grüße  Welt", " Grüße Welt", "Grüße Welt ", "Grüße\rWelt", None):
        assert not _same_message_text(changed, "Grüße Welt")


def _verify_received(
    client: httpx.Client,
    account_id: str,
    conversation_id: str,
    sender_user_id: str,
    expected_text: str,
    before_ids: set[str],
) -> None:
    page = _conversation(client, account_id, conversation_id)
    if str(page.get("recipient_user_id")) != sender_user_id:
        pytest.fail("Gespräch gehört nicht zum erwarteten eigenen Gegenaccount")
    received = [
        message
        for message in page.get("messages", [])
        if str(message.get("sender_user_id")) == sender_user_id
        and str(message.get("message_id")) not in before_ids
    ]
    if not any(
        _same_message_text(message.get("text"), expected_text) for message in received
    ):
        pytest.fail("Der neue Testtext wurde beim eigenen Empfänger nicht gefunden")
    images = [image for message in received for image in message.get("images", [])]
    if not images:
        pytest.fail("Das neue Testbild wurde beim eigenen Empfänger nicht gefunden")
    image = client.get(
        f"/messaging/accounts/{account_id}/images/{images[-1]['image_id']}"
    )
    if image.status_code != 200 or not image.headers.get("content-type", "").startswith(
        "image/"
    ):
        pytest.fail("Archiviertes Testbild lässt sich nicht lesen")
    if not image.content:
        pytest.fail("Archiviertes Testbild ist leer")
    archive = _conversation(client, account_id, conversation_id, "archive")
    if archive.get("source") != "archive":
        pytest.fail("Archivabruf ist nicht eindeutig als Archiv gekennzeichnet")
    if not any(
        _same_message_text(message.get("text"), expected_text)
        for message in archive.get("messages", [])
    ):
        pytest.fail("Testtext fehlt im dauerhaften Archiv")


@pytest.mark.live_messaging
def test_own_accounts_text_and_image_round_trip() -> None:
    """A → eigene Anzeige von B → B liest → B antwortet → A liest/Archiv."""
    account_a = _required(
        "KLEINANZEIGEN_LIVE_MESSAGING_ACCOUNT_A", r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
    )
    account_b = _required(
        "KLEINANZEIGEN_LIVE_MESSAGING_ACCOUNT_B", r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
    )
    listing_id = _required(
        "KLEINANZEIGEN_LIVE_MESSAGING_LISTING_ID", r"\d+(?:-\d+-\d+)?"
    )
    base_url = os.environ.get("KLEINANZEIGEN_LIVE_BASE_URL", "http://127.0.0.1:8000")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        pytest.fail("Nachrichten-Live-Tests benötigen eine lokale HTTP-Backendadresse")
    run_id = _run_id()
    with httpx.Client(base_url=base_url, timeout=90, follow_redirects=False) as client:
        user_a, user_b = _require_policy(client, account_a, account_b, listing_id)
        _verify_listing_owner(client, listing_id, user_b, run_id)
        conversation_b = _find_conversation(client, account_b, user_a, listing_id)
        before_b = (
            {
                str(m.get("message_id"))
                for m in _conversation(client, account_b, conversation_b).get(
                    "messages", []
                )
            }
            if conversation_b
            else set()
        )
        upload_a = _json(
            client.post(
                f"/messaging/accounts/{account_a}/attachments",
                content=_test_png(),
                headers={
                    "Content-Type": "image/png",
                    "X-Filename": "eigener-nachrichtentest.png",
                },
            )
        )
        _require_policy(client, account_a, account_b, listing_id)
        text_a = f"Eigener Funktionstest {run_id}: Grüße, Äpfel und Öl.\nBitte nur im Testaccount beachten."
        first_payload = {
            "listing_id": listing_id,
            "text": text_a,
            "attachment_ids": [upload_a["attachment_id"]],
            "idempotency_key": f"own-accounts-first-{run_id}",
        }
        accepted_a = _json(
            client.post(
                f"/messaging/accounts/{account_a}/start-conversation",
                json=first_payload,
            )
        )
        sent_a = _wait_for_send(client, account_a, accepted_a)
        _require_policy(client, account_a, account_b, listing_id)
        replay = _json(
            client.post(
                f"/messaging/accounts/{account_a}/start-conversation",
                json=first_payload,
            )
        )
        if replay.get("operation_id") != accepted_a["operation_id"]:
            pytest.fail("Idempotente Wiederholung erzeugte einen anderen Auftrag")
        conversation_a = str(sent_a["conversation_id"])
        conversation_b = _find_conversation(client, account_b, user_a, listing_id)
        if not conversation_b:
            pytest.fail(
                "Erstkontakt fehlt im eigenen Empfängeraccount; nicht neu senden"
            )
        _verify_received(client, account_b, conversation_b, user_a, text_a, before_b)
        before_a = {
            str(m.get("message_id"))
            for m in _conversation(client, account_a, conversation_a).get(
                "messages", []
            )
        }
        upload_b = _json(
            client.post(
                f"/messaging/accounts/{account_b}/attachments",
                content=_test_png(),
                headers={
                    "Content-Type": "image/png",
                    "X-Filename": "eigene-testantwort.png",
                },
            )
        )
        _require_policy(client, account_a, account_b, listing_id)
        text_b = f"Eigene Testantwort {run_id}: Rückmeldung mit Bild – Grüße!"
        accepted_b = _json(
            client.post(
                f"/messaging/accounts/{account_b}/send-message",
                json={
                    "conversation_id": conversation_b,
                    "text": text_b,
                    "attachment_ids": [upload_b["attachment_id"]],
                    "idempotency_key": f"own-accounts-reply-{run_id}",
                },
            )
        )
        _wait_for_send(client, account_b, accepted_b)
        _verify_received(client, account_a, conversation_a, user_b, text_b, before_a)
