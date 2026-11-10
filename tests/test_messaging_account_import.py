from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "archive_account", Path(__file__).resolve().parents[1] / "script/archive_messaging_account.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def page(collection, items, **extra):
    return {"account_id": "own", "source": "live", collection: items, "has_more": False, "complete": True,
            "provider_context": {"snapshots": [], "latest_checks": [
                {"kind": kind, "status": "not_present"} for kind in ("listing_provider", "listing_imprint", "profile")
            ]}, **extra}


def run(pages, report=None):
    calls, saves = [], []

    def read(path, params):
        calls.append((path, params))
        result = pages.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    result = module.import_account(read, "own", report or {}, lambda r: saves.append(deepcopy(r)), lambda _: None)
    return result, calls, saves


def test_all_inbox_and_message_pages_are_consumed_without_sending():
    result, calls, saves = run([
        page("conversations", [{"conversation_id": "c1"}], has_more=True, next_cursor="inbox-next"),
        page("conversations", [{"conversation_id": "c2"}]),
        page("messages", [{"message_id": "m2", "text": "PRIVATE"}], conversation_id="c1", has_more=True, next_cursor="older"),
        page("messages", [{"message_id": "m1"}], conversation_id="c1"),
        page("messages", [], conversation_id="c2"),
    ])
    assert result["state"] == "complete"
    assert result["conversation_ids"] == ["c1", "c2"]
    assert result["conversations"]["c1"]["message_ids"] == ["m2", "m1"]
    assert calls[1][1]["cursor"] == "inbox-next"
    assert calls[3][1]["cursor"] == "older"
    assert "PRIVATE" not in repr(saves)
    assert all("/conversations" in path for path, _ in calls)


def test_rate_limit_pauses_and_resume_skips_completed_conversation():
    result, _, _ = run([
        page("conversations", [{"conversation_id": "c1"}, {"conversation_id": "c2"}]),
        page("messages", [], conversation_id="c1"),
        module.ImportFailure("upstream_rate_limited"),
    ])
    assert result["state"] == "paused"
    resumed, calls, _ = run([page("messages", [{"message_id": "m3"}], conversation_id="c2")], result)
    assert resumed["state"] == "complete"
    assert len(calls) == 1 and calls[0][0].endswith("/c2")


@pytest.mark.parametrize("extra", [
    {"unsupported_message_count": 1, "complete": False},
    {"messages": [{"message_id": "m1", "images": [{"remote_id": "m1:0", "unavailable": True}]}]},
])
def test_incomplete_content_never_becomes_full_success(extra):
    result, _, _ = run([
        page("conversations", [{"conversation_id": "c1"}]),
        {**page("messages", [], conversation_id="c1"), **extra},
    ])
    assert result["state"] == "partial"
    assert result["conversations"]["c1"]["pages_complete"]


def test_cross_account_response_is_rejected_and_stops():
    result, _, _ = run([{**page("conversations", []), "account_id": "other"}])
    assert result["state"] == "paused"
    assert result["error_code"] == "account_mismatch"
    assert not result.get("inventory_complete")


def test_incomplete_inbox_cannot_claim_all_chats_imported():
    result, calls, _ = run([page("conversations", [{"conversation_id": "c1"}], complete=False)])
    assert result["state"] == "paused"
    assert result["error_code"] == "inventory_incomplete"
    assert not result.get("inventory_complete")
    assert len(calls) == 1


@pytest.mark.parametrize("context", [None, {}, {"latest_checks": [{"kind": "profile", "status": "unavailable"}]}])
def test_missing_provider_context_remains_partial(context):
    result, _, _ = run([
        page("conversations", [{"conversation_id": "c1"}]),
        page("messages", [], conversation_id="c1", provider_context=context),
    ])
    assert result["state"] == "partial"
    assert result["conversations"]["c1"]["messages_complete"]


def test_provider_block_keeps_fetched_messages_but_stops_further_requests():
    result, calls, _ = run([
        page("conversations", [{"conversation_id": "c1"}, {"conversation_id": "c2"}]),
        page("messages", [{"message_id": "m1"}], conversation_id="c1", access_warning={"code": "upstream_blocked"}),
    ])
    assert result["state"] == "paused"
    assert result["conversations"]["c1"]["message_ids"] == ["m1"]
    assert len(calls) == 2


@pytest.mark.parametrize("error_field", ["provider_context_error", "system_events_error"])
def test_earlier_page_errors_survive_a_successful_final_page(error_field):
    result, _, _ = run([
        page("conversations", [{"conversation_id": "c1"}]),
        page("messages", [{"message_id": "m2"}], conversation_id="c1", has_more=True,
             next_cursor="older", **{error_field: {"code": "invalid_context"}}),
        page("messages", [{"message_id": "m1"}], conversation_id="c1"),
    ])
    assert result["state"] == "partial"
    assert result["conversations"]["c1"][error_field] is True


def test_repeated_cursor_cannot_create_infinite_import():
    result, calls, _ = run([
        page("conversations", [{"conversation_id": "c1"}]),
        page("messages", [{"message_id": "m1"}], conversation_id="c1", has_more=True, next_cursor="same"),
        page("messages", [{"message_id": "m1"}], conversation_id="c1", has_more=True, next_cursor="same"),
    ])
    assert result["state"] == "partial"
    assert result["conversations"]["c1"]["error_code"] == "cursor_repeated"
    assert len(calls) == 3


@pytest.mark.parametrize("url", ["https://127.0.0.1:80", "http://evil.test:80", "http://127.0.0.1.evil.test:80", "http://127.0.0.1:80/path", "http://user@127.0.0.1:80"])
def test_nonlocal_and_ambiguous_api_addresses_rejected(url):
    with pytest.raises(ValueError):
        module.local_base(url)


def test_progress_file_is_private_and_atomic(tmp_path):
    path = tmp_path / "reports" / "run.json"
    module.save_report(path, {"state": "running"})
    module.save_report(path, {"state": "complete"})
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not list(path.parent.glob(".archive-progress-*"))
