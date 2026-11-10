"""Bounded, identity-bound public provider information for the chat archive."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from urllib.parse import parse_qsl, urlsplit

from .contracts import MessagingError


KINDS = frozenset({"listing_provider", "listing_imprint", "profile"})
STATUSES = frozenset({
    "captured", "not_present", "unavailable", "identity_mismatch", "not_counterpart",
})
ERROR_CODES = frozenset({
    "request_failed", "page_unavailable", "identity_unverified", "identity_mismatch",
    "provider_context_invalid", "access_blocked", "content_missing", "timeout",
})
PROVIDER_GUARDS = frozenset({
    "listing_url", "provider_root", "conflicting_numeric_id",
    "no_numeric_or_business_link", "pro_url", "pro_identity_components",
})
MAX_TEXT = 20_000
STRUCTURE_LIMITS = {
    "provider_nodes": 2000, "profile_links": 2000, "imprint_nodes": 2000,
    "imprint_triggers": 2000, "provider_text_chars": 20000,
    "imprint_text_chars": 20000, "body_bytes": 2_000_000,
}


def _invalid():
    raise MessagingError(
        "provider_context_invalid",
        "Die Anbieterinformationen konnten nicht eindeutig dem Gespräch zugeordnet werden.",
        409,
    )


def _user(value):
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value)


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        _invalid()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            _invalid()
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        _invalid()


def _source(value, kind, user_id, listing_id):
    if not isinstance(value, str) or len(value) > 2048:
        _invalid()
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"www.kleinanzeigen.de", "kleinanzeigen.de"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.fragment
            or "\\" in value
            or any(ord(char) < 33 for char in value)
        ):
            _invalid()
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (ValueError, TypeError):
        _invalid()
    if kind.startswith("listing_"):
        target = re.fullmatch(
            r"/s-anzeige/(?:[^/]+/)?([0-9]{1,20})(?:-[0-9]+){0,2}/?", parsed.path
        )
        if (
            target is None or target[1] != listing_id
            or pairs
        ):
            _invalid()
    elif not (
        (parsed.path in {"/s-bestandsliste.html", "/s-anzeigen-des-nutzers.html"}
         and pairs == [("userId", user_id)])
        or (parsed.path in {"/s-anzeigen-des-nutzers/" + user_id, "/s-anzeigen-des-nutzers/" + user_id + "/"} and not pairs)
        or (re.fullmatch(r"/pro/[A-Za-z0-9._~-]+/?", parsed.path) and not pairs)
    ):
        _invalid()
    return value


def validate_provider_context(value, recipient_user_id, listing_id):
    """Return safe observations; never trust a context's own account binding."""
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or not _user(recipient_user_id)
        or value.get("recipient_user_id") != recipient_user_id
        or value.get("listing_id") != listing_id
        or (listing_id is not None and not _user(listing_id))
    ):
        _invalid()
    sections = value.get("sections")
    if not isinstance(sections, list) or len(sections) > len(KINDS):
        _invalid()
    seen, result = set(), []
    for section in sections:
        if not isinstance(section, dict):
            _invalid()
        kind, status = section.get("kind"), section.get("status")
        if (
            not isinstance(kind, str) or kind not in KINDS or kind in seen
            or not isinstance(status, str) or status not in STATUSES
        ):
            _invalid()
        seen.add(kind)
        captured_at = _timestamp(section.get("captured_at"))
        entry = {
            "kind": kind,
            "status": status,
            "recipient_user_id": recipient_user_id,
            "listing_id": listing_id,
            "captured_at": captured_at,
        }
        source_url = section.get("source_url")
        if source_url is not None:
            entry["source_url"] = _source(source_url, kind, recipient_user_id, listing_id)
        if status == "captured":
            if (
                section.get("user_id") != recipient_user_id
                or (kind.startswith("listing_") and (
                    listing_id is None or section.get("listing_id") != listing_id
                ))
                or source_url is None
            ):
                _invalid()
            content, title = section.get("text"), section.get("title", "")
            if (
                not isinstance(content, str)
                or not content.strip()
                or len(content) > MAX_TEXT
                or not isinstance(title, str)
                or len(title) > 300
                or any(ord(char) < 32 and char not in "\n\r\t" for char in content + title)
                or type(section.get("truncated", False)) is not bool
            ):
                _invalid()
            entry.update(
                user_id=recipient_user_id, text=content, title=title,
                truncated=section.get("truncated", False),
            )
        else:
            # Failed/foreign observations never carry page content into storage.
            error_code = section.get("error_code")
            if isinstance(error_code, str) and error_code in ERROR_CODES:
                entry["error_code"] = error_code
            provider_guard = section.get("provider_guard")
            if isinstance(provider_guard, str) and provider_guard in PROVIDER_GUARDS:
                entry["provider_guard"] = provider_guard
            structure = section.get("structure")
            if isinstance(structure, dict):
                safe = {key: structure[key] for key, maximum in STRUCTURE_LIMITS.items()
                        if type(structure.get(key)) is int and 0 <= structure[key] <= maximum}
                if safe:
                    entry["structure"] = safe
        result.append(entry)
    return result
