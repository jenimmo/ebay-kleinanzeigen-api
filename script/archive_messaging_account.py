"""Explicit, resumable read-only account import through the local messaging API.

The backend owns browser limits and durable storage. This file records progress,
never message bodies, and has no sending or login operation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener, HTTPRedirectHandler


ROOT = Path(__file__).resolve().parents[1]
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
STOP_ERRORS = frozenset({
    "reauth_required", "account_mismatch", "account_unverified",
    "upstream_blocked", "upstream_rate_limited", "outbound_circuit_open",
    "safari_bridge_timeout", "safari_tab_missing", "safari_recovery_unavailable",
    "transport_error",
})


def now():
    return datetime.now(timezone.utc).isoformat()


class ImportFailure(Exception):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code) else "invalid_response"
        super().__init__(self.code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_base(value):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "http" and ipaddress.ip_address(parsed.hostname).is_loopback
                 and parsed.port and not parsed.username and not parsed.password
                 and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("Nur eine numerische lokale HTTP-Adresse ist zulässig.")
    return value.rstrip("/")


def api_reader(base):
    base = local_base(base)
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def read(path, params):
        url = base + path + "?" + urlencode(params)
        try:
            with opener.open(Request(url, method="GET", headers={"Accept": "application/json"}), timeout=90) as response:
                body = response.read(16 * 1024 * 1024 + 1)
            if len(body) > 16 * 1024 * 1024:
                raise ImportFailure("response_too_large")
            result = json.loads(body)
        except HTTPError as error:
            try:
                detail = json.loads(error.read(65536)).get("detail", {})
                code = detail.get("code") if isinstance(detail, dict) else None
            except (ValueError, AttributeError):
                code = None
            raise ImportFailure(code) from None
        except (URLError, TimeoutError, OSError):
            raise ImportFailure("transport_error") from None
        except (ValueError, UnicodeError):
            raise ImportFailure("invalid_response") from None
        if not isinstance(result, dict):
            raise ImportFailure("invalid_response")
        return result

    return read


def save_report(path, report):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Berichtspfad darf kein symbolischer Link sein.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    report["updated_at"] = now()
    fd, temporary = tempfile.mkstemp(prefix=".archive-progress-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_page(data, account_id, collection, conversation_id=None):
    if data.get("account_id") != account_id or data.get("source") != "live":
        raise ImportFailure("account_mismatch")
    if conversation_id is not None and data.get("conversation_id") != conversation_id:
        raise ImportFailure("conversation_mismatch")
    if not isinstance(data.get(collection), list) or type(data.get("has_more")) is not bool:
        raise ImportFailure("invalid_response")
    if data["has_more"] and (not isinstance(data.get("next_cursor"), str) or not data["next_cursor"]):
        raise ImportFailure("invalid_pagination")


def import_account(read, account_id, report, save, progress=print):
    if not ACCOUNT.fullmatch(account_id):
        raise ValueError("Ungültige Account-ID.")
    if report and report.get("account_id") != account_id:
        raise ValueError("Der Bericht gehört zu einem anderen Account.")
    report.setdefault("schema_version", 1)
    report.setdefault("account_id", account_id)
    report.setdefault("started_at", now())
    report.setdefault("conversations", {})
    report["state"] = "running"
    report.pop("error_code", None)
    path = "/messaging/accounts/" + quote(account_id, safe="") + "/conversations"
    try:
        if not report.get("inventory_complete"):
            cursor, cursors, listed = None, set(), []
            while True:
                params = {"source": "live", "limit": 200, "unread_only": "false"}
                if cursor:
                    params["cursor"] = cursor
                page = read(path, params)
                validate_page(page, account_id, "conversations")
                for item in page["conversations"]:
                    cid = item.get("conversation_id") if isinstance(item, dict) else None
                    if not isinstance(cid, str) or not ID.fullmatch(cid):
                        raise ImportFailure("invalid_response")
                    if cid not in listed:
                        listed.append(cid)
                if not page["has_more"]:
                    if page.get("complete") is not True:
                        raise ImportFailure("inventory_incomplete")
                    break
                cursor = page["next_cursor"]
                if cursor in cursors:
                    raise ImportFailure("cursor_repeated")
                cursors.add(cursor)
            report["conversation_ids"] = listed
            report["inventory_complete"] = True
            save(report)
        total = len(report["conversation_ids"])
        for index, cid in enumerate(report["conversation_ids"], 1):
            previous = report["conversations"].get(cid, {})
            if previous.get("pages_complete"):
                continue
            cursor, cursors = None, set()
            item = {"pages": 0, "message_ids": [], "system_event_ids": [], "image_references": {}, "unsupported_message_count": 0}
            report["conversations"][cid] = item
            try:
                while True:
                    params = {"source": "live", "limit": 50}
                    if cursor:
                        params["cursor"] = cursor
                    page = read(path + "/" + quote(cid, safe=""), params)
                    validate_page(page, account_id, "messages", cid)
                    item["pages"] += 1
                    for message in page["messages"]:
                        mid = message.get("message_id")
                        if not isinstance(mid, str) or not ID.fullmatch(mid):
                            raise ImportFailure("invalid_response")
                        if mid not in item["message_ids"]:
                            item["message_ids"].append(mid)
                        for attachment in message.get("images", []):
                            key = str(attachment.get("remote_id") or attachment.get("image_id"))
                            item["image_references"][key] = bool(attachment.get("image_id"))
                    item["unsupported_message_count"] = max(item["unsupported_message_count"], page.get("unsupported_message_count", 0))
                    for error_field in ("provider_context_error", "system_events_error"):
                        if page.get(error_field):
                            item[error_field] = True
                    for event in page.get("system_events", []):
                        event_id = event.get("event_id")
                        if not isinstance(event_id, str) or not ID.fullmatch(event_id):
                            raise ImportFailure("invalid_response")
                        if event_id not in item["system_event_ids"]:
                            item["system_event_ids"].append(event_id)
                    context = page.get("provider_context", {})
                    if context:
                        item["provider_snapshot_count"] = len(context.get("snapshots", []))
                        item["provider_checks"] = [
                            {k: check.get(k) for k in ("kind", "status", "error_code") if k in check}
                            for check in context.get("latest_checks", [])
                        ]
                        checks = {check.get("kind"): check.get("status") for check in context.get("latest_checks", [])}
                        item["provider_context_complete"] = (
                            set(checks) == {"listing_provider", "listing_imprint", "profile"}
                            and all(status in {"captured", "not_present", "not_counterpart"} for status in checks.values())
                            and not item.get("provider_context_error")
                        )
                    warning = page.get("access_warning", {})
                    if warning.get("code") in STOP_ERRORS:
                        save(report)
                        raise ImportFailure(warning["code"])
                    if not page["has_more"]:
                        item["pages_complete"] = True
                        item["messages_complete"] = page.get("complete") is True and item["unsupported_message_count"] == 0
                        item["images_complete"] = all(item["image_references"].values())
                        item["complete"] = (item["messages_complete"] and item["images_complete"]
                                            and item.get("provider_context_complete", False)
                                            and not item.get("system_events_error"))
                        save(report)
                        break
                    cursor = page["next_cursor"]
                    if cursor in cursors:
                        raise ImportFailure("cursor_repeated")
                    cursors.add(cursor)
                    save(report)
            except ImportFailure as error:
                item["error_code"] = error.code
                save(report)
                if error.code in STOP_ERRORS:
                    raise
            progress(f"Archiv: {index}/{total} Gespräche bearbeitet.")
        report["state"] = "complete" if all(
            report["conversations"].get(cid, {}).get("complete") for cid in report["conversation_ids"]
        ) else "partial"
    except ImportFailure as error:
        report["state"] = "paused"
        report["error_code"] = error.code
    save(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--base-url")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    base = args.base_url or json.loads((ROOT / "data/.mcp-runtime/backend.json").read_text())["base_url"]
    report = json.loads(args.report.read_text()) if args.report.exists() else {}
    result = import_account(api_reader(base), args.account_id, report, lambda value: save_report(args.report, value))
    print(json.dumps({"state": result["state"], "conversations": len(result.get("conversation_ids", [])), "report": str(args.report.absolute())}, ensure_ascii=False))
    return 0 if result["state"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
