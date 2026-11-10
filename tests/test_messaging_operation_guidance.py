"""Technical Safari connection failures must not request another password login."""

import pytest

from messaging.service import _public_operation


@pytest.mark.parametrize(
    "code",
    [
        "safari_navigation_timeout",
        "safari_timeout",
        "safari_unavailable",
        "identity_unverified",
        "identity_page_required",
        "invalid_bridge_reply",
        "invalid_page_reply",
        "command_expired",
    ],
)
def test_safari_connection_error_preserves_state_and_gives_technical_next_action(code):
    operation = {"kind": "connect", "state": "reauth_required", "error": {"code": code}}
    result = _public_operation(operation)
    assert result["state"] == "reauth_required"
    assert "Safari-Erweiterung und lokale Verbindung prüfen" in result["next_action"]
    assert "Kontoanmeldung muss nicht wiederholt werden" in result["next_action"]
    assert "connect_account" in result["next_action"]
    assert operation == {
        "kind": "connect",
        "state": "reauth_required",
        "error": {"code": code},
    }


@pytest.mark.parametrize(
    "kind,state,code",
    [
        ("connect", "reauth_required", "reauth_required"),
        ("connect", "reauth_required", "account_mismatch"),
        ("connect", "blocked", "upstream_blocked"),
        ("send", "outcome_unknown", "safari_timeout"),
        ("send", "reauth_required", "safari_timeout"),
    ],
)
def test_technical_guidance_does_not_replace_auth_or_uncertain_send_instructions(
    kind, state, code
):
    result = _public_operation({"kind": kind, "state": state, "error": {"code": code}})
    assert "Kontoanmeldung muss nicht wiederholt werden" not in result["next_action"]
    if state == "outcome_unknown":
        assert "keinen neuen Versandauftrag" in result["next_action"]


def test_expired_pairing_requests_linking_instead_of_repeating_login():
    result = _public_operation(
        {
            "kind": "connect",
            "state": "reauth_required",
            "error": {"code": "safari_pairing_timeout"},
        }
    )
    assert "Diesen Tab verbinden" in result["next_action"]
    assert "connect_account" in result["next_action"]
    assert "Kontoanmeldung muss nicht wiederholt werden" in result["next_action"]
