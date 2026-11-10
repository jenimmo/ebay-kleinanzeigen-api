"""Native-messaging bridge for normal, separately authenticated Safari profiles.

The native extension supplies the profile identifier. Cookies and credentials
never leave Safari. Commands are removed from the delivery queue before being
returned, and a lost response is never an invitation to deliver them again.
"""

from __future__ import annotations

import asyncio
import base64
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import uuid

from .contracts import MAX_IMAGE_BYTES, MessagingError, validate_id
from .diagnostics import image_structure
from .image_evidence import image_preview, pixel_proof, receipt_evidence
from utils.input_validation import normalize_listing_id
from utils.outbound_control import UpstreamBlocked, UpstreamRateLimited

CHUNK_BYTES = 32 * 1024
MAX_TRANSFER_BYTES = 40 * 1024 * 1024
_METHODS = {
    "identify",
    "recover_tab",
    "list_conversations",
    "get_conversation",
    "prepare_send",
    "send_step",
    "confirm_send",
    "inspect_ui",
}
_ERRORS = {
    "reauth_required",
    "account_mismatch",
    "recipient_unverified",
    "recipient_mismatch",
    "target_mismatch",
    "conversation_not_found",
    "conversation_mismatch",
    "ui_unsupported",
    "images_unsupported",
    "text_unsupported",
    "draft_not_empty",
    "send_unavailable",
    "outcome_unknown",
    "upstream_blocked",
    "upstream_rate_limited",
    "image_unavailable",
    "invalid_cursor",
    "command_expired",
    "response_too_large",
    "invalid_bridge_reply",
    "safari_navigation_timeout",
    "safari_content_timeout",
    "safari_tab_missing",
    "safari_tab_ambiguous",
    "safari_recovery_unavailable",
    "invalid_page_reply",
    "unsafe_tab",
    "invalid_command",
    "account_unverified",
    "identity_unverified",
    "identity_page_required",
    "conversation_unverified",
    "message_sender_unverified",
    "message_text_unverified",
    "composer_not_ready",
    "message_too_large",
    "draft_present",
}


def _error(code, message, status=409):
    return MessagingError(code, message, status)


def _profile(value):
    if value == "default":
        return value
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise _error(
            "invalid_safari_profile", "Ungültiges Safari-Profil.", 422
        ) from None


def _user(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        raise _error(
            "identity_unverified", "Die Kleinanzeigen-Nutzer-ID ist nicht eindeutig."
        )
    return value


def _structure_path(value):
    """Keep known routes and numeric listing IDs; omit slugs and all queries."""
    if not isinstance(value, str):
        return None
    if value in {
        "/m-nachrichten.html",
        "/m-meine-anzeigen.html",
        "/m-einstellungen.html",
        "/m-mein-konto.html",
        "/portal/anzeigen",
        "/portal/nachrichten",
        "/s-bestandsliste.html",
    }:
        return value
    if re.fullmatch(r"/s-anzeigen-des-nutzers/[0-9]{1,20}", value):
        return value
    if re.fullmatch(r"/zur-anzeige/[0-9]{6,20}", value):
        return value
    listing = re.fullmatch(
        r"/s-anzeige/(?:[^/?#]+/)?([0-9]{6,20}(?:-[0-9]{1,8}){0,2})/?", value
    )
    return "/s-anzeige/" + listing[1] if listing else None


def _content_revision(value):
    if isinstance(value, str) and re.fullmatch(
        r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", value
    ):
        return value
    return None


def _reader_structure(value):
    """Whitelist field shapes for one failed list read; never include values."""
    if not isinstance(value, dict) or value.get("operation") != "list":
        return None
    stages = {
        "profile_request", "token_request", "list_request", "list_shape",
        "list_meta", "list_item", "list_unread", "list_timestamp",
    }
    reasons = {
        "network", "http_status", "body_unavailable", "content_type", "body_read",
        "response_size", "json_parse", "duplicate_root_key", "root_type",
        "conversations_type", "conversation_count", "metadata_type", "num_found",
        "page_number", "page_size", "found_count", "empty_page", "item_unread",
        "item_timestamp", "item_normalization",
    }
    if (
        not isinstance(value.get("stage"), str)
        or value["stage"] not in stages
        or not isinstance(value.get("reason"), str)
        or value["reason"] not in reasons
    ):
        return None
    result = {key: value[key] for key in ("operation", "stage", "reason")}
    types = {"missing", "null", "array", "object", "string", "number", "boolean", "other"}
    type_fields = {
        "root_types": ("conversations", "_meta", "_links", "lastModified"),
        "meta_types": ("numFound", "pageNum", "pageSize"),
        "item_types": (
            "id", "adId", "role", "userIdBuyer", "userIdSeller", "unread",
            "unreadMessagesCount", "adTitle", "receivedDate",
        ),
    }
    for name, keys in type_fields.items():
        fields = value.get(name)
        if isinstance(fields, dict):
            result[name] = {
                key: fields[key]
                for key in keys
                if isinstance(fields.get(key), str) and fields[key] in types
            }
    if isinstance(value.get("body_type"), str) and value["body_type"] in types:
        result["body_type"] = value["body_type"]
    if isinstance(value.get("timestamp_format"), str) and value["timestamp_format"] in {
        "iso_zoned", "iso_unzoned", "iso_offset_compact", "iso_space_zoned",
        "numeric_epoch_ms", "numeric_epoch_s", "date_only", "other", "missing",
    }:
        result["timestamp_format"] = value["timestamp_format"]
    for name, minimum, maximum in (
        ("http_status", 100, 599), ("requested_page", 0, 999999),
        ("requested_size", 1, 200), ("conversation_count", 0, 1000),
        ("item_index", 0, 199),
    ):
        number = value.get(name)
        if type(number) is int and minimum <= number <= maximum:
            result[name] = number
    metadata = value.get("meta_values")
    if isinstance(metadata, dict):
        result["meta_values"] = {
            key: metadata[key]
            for key in ("numFound", "pageNum", "pageSize")
            if type(metadata.get(key)) is int and 0 <= metadata[key] <= 1000000
        }
    for name in (
        "json_mime", "duplicate_root_key", "conversation_count_limited",
        "timestamp_parseable", "timestamp_iso_zoned",
    ):
        if type(value.get(name)) is bool:
            result[name] = value[name]
    return result


def _own_component_schema(value):
    """Describe only bounded, known identity-source structure, never values."""
    if not isinstance(value, list):
        return None
    sources = []
    path_keys = {
        "props",
        "pageProps",
        "data",
        "user",
        "currentUser",
        "loggedInUser",
        "userProfile",
        "profile",
        "account",
        "id",
        "userId",
        "loggedIn",
        "isLoggedIn",
        "[]",
    }
    attribute_names = {
        "id",
        "class",
        "type",
        "role",
        "data-testid",
        "data-user-id",
        "data-userid",
        "data-user_id",
        "data-current-user-id",
        "data-logged-in-user-id",
        "data-current-user",
        "data-user",
        "data-account-id",
        "data-account",
        "data-profile-id",
        "data-profile",
        "data-id",
        "data-uid",
        "component-url",
        "component-export",
        "client",
        "props",
        "ssr",
        "uid",
        "aria-label",
    }
    for source in value[:2]:
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("source"), str)
            or source["source"] not in {"rendered", "ssr"}
            or not isinstance(source.get("nodes"), list)
        ):
            continue
        safe_source = {"source": source["source"], "nodes": []}
        if type(source.get("scan_limited")) is bool:
            safe_source["scan_limited"] = source["scan_limited"]
        for node in source["nodes"][:12]:
            if (
                not isinstance(node, dict)
                or not isinstance(node.get("tag"), str)
                or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", node["tag"])
            ):
                continue
            safe_node = {"tag": node["tag"]}
            attributes = node.get("attribute_names")
            if isinstance(attributes, list):
                safe_node["attribute_names"] = [
                    name
                    for name in attributes[:24]
                    if isinstance(name, str) and name in attribute_names
                ]
            for name in (
                "in_header",
                "matches_account",
                "matches_profile",
                "matches_user",
                "astro",
                "payload_present",
                "payload_valid_json",
            ):
                if type(node.get(name)) is bool:
                    safe_node[name] = node[name]
            kind = node.get("payload_kind")
            if isinstance(kind, str) and kind in {"astro_props", "json_script", "none"}:
                safe_node["payload_kind"] = kind
            shapes = node.get("payload_shape")
            if isinstance(shapes, list):
                safe_node["payload_shape"] = [
                    {"path": shape["path"], "type": shape["type"]}
                    for shape in shapes[:24]
                    if isinstance(shape, dict)
                    and isinstance(shape.get("path"), list)
                    and len(shape["path"]) <= 5
                    and all(
                        isinstance(key, str) and key in path_keys
                        for key in shape["path"]
                    )
                    and isinstance(shape.get("type"), str)
                    and shape["type"]
                    in {"object", "array", "string", "number", "boolean", "null"}
                ]
            safe_source["nodes"].append(safe_node)
        sources.append(safe_source)
    return sources


def _sanitize_structure(result):
    """A second fixed allowlist prevents future extension changes leaking UI text."""
    if not isinstance(result, dict) or not isinstance(result.get("nodes"), list):
        raise _error("ui_unsupported", "Safari lieferte keine gültige DOM-Struktur.")
    nodes = []
    identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
    for node in result["nodes"][:100]:
        if (
            not isinstance(node, dict)
            or not isinstance(node.get("tag"), str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", node["tag"])
        ):
            continue
        safe = {"tag": node["tag"]}
        if isinstance(node.get("profile_scope"), str) and node["profile_scope"] in {
            "userprofile-vip", "userprofile"
        }:
            safe["profile_scope"] = node["profile_scope"]
        for name in ("visible", "disabled", "has_direct_text"):
            if type(node.get(name)) is bool:
                safe[name] = node[name]
        if type(node.get("parent_index")) is int and -1 <= node["parent_index"] < 100:
            safe["parent_index"] = node["parent_index"]
        shadow_count = node.get("shadow_child_count")
        if type(shadow_count) is int and 0 <= shadow_count <= 100:
            safe["shadow_child_count"] = shadow_count
        if isinstance(node.get("children_tags"), list):
            safe["children_tags"] = [
                tag
                for tag in node["children_tags"][:12]
                if isinstance(tag, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", tag)
            ]
        attributes = node.get("attributes", [])
        if isinstance(attributes, list):
            safe["attributes"] = [
                value
                for value in attributes[:64]
                if isinstance(value, str)
                and re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:.-]{0,63}", value)
                and not value.lower().startswith("on")
            ]
        for name in (
            "id",
            "data-testid",
            "data-conversation-id",
            "data-active-conversation-id",
            "data-message-id",
            "conversationId",
            "messageId",
        ):
            value = node.get(name)
            if isinstance(value, str) and identifier.fullmatch(value):
                safe[name] = value
        if isinstance(node.get("userId"), str) and re.fullmatch(
            r"[0-9]{1,20}", node["userId"]
        ):
            safe["userId"] = node["userId"]
        for name, allowed in {
            "data-direction": {"inbound", "outbound", "INBOUND", "OUTBOUND"},
            "data-auto-send": {"true", "false"},
            "type": {"button", "submit", "file", "text", "hidden", "textarea"},
        }.items():
            if isinstance(node.get(name), str) and node[name] in allowed:
                safe[name] = node[name]
        maxlength = node.get("maxlength")
        if isinstance(maxlength, str) and re.fullmatch(r"[0-9]{1,6}", maxlength):
            safe["maxlength"] = maxlength
        accept = node.get("accept")
        if (
            isinstance(accept, str)
            and len(accept) <= 120
            and all(
                re.fullmatch(
                    r"(?:image/(?:[a-zA-Z0-9.+_-]+|\*)|\.[a-zA-Z0-9]+)", token.strip()
                )
                for token in accept.split(",")
            )
        ):
            safe["accept"] = accept
        path = _structure_path(node.get("link_path"))
        if path is not None:
            safe["link_path"] = path
        frame_path = node.get("frame_path")
        if isinstance(frame_path, str) and frame_path in {
            "/m-meine-anzeigen.html",
            "/portal/anzeigen",
            "/m-nachrichten.html",
            "/portal/nachrichten",
        }:
            safe["frame_path"] = frame_path
        nodes.append(safe)
    safe_result = {
        "page": _structure_path(result.get("page")),
        "nodes": nodes,
        "node_count": len(nodes),
        "truncated": len(result["nodes"]) > 100 or result.get("truncated") is True,
    }
    for name in (
        "empty_heading_in_main",
        "empty_text_in_main",
        "empty_heading_in_document",
        "empty_text_in_document",
        "listing_recipient_verified",
    ):
        if type(result.get(name)) is bool:
            safe_result[name] = result[name]
    shadow_count = result.get("open_shadow_host_count")
    if type(shadow_count) is int and 0 <= shadow_count <= 100:
        safe_result["open_shadow_host_count"] = shadow_count
    revision = _content_revision(result.get("content_revision"))
    if revision is not None:
        safe_result["content_revision"] = revision
    variants = result.get("empty_variants")
    if isinstance(variants, dict):
        safe_result["empty_variants"] = {
            name: variants[name]
            for name in (
                "intro_text_content",
                "detail_text_content",
                "intro_inner_text",
                "detail_inner_text",
                "detail_without_whitespace",
                "detail_without_terminal_dot",
                "intro_aria_alt",
                "detail_aria_alt",
            )
            if type(variants.get(name)) is bool
        }
    return safe_result


def _identity_structure(value):
    """Safe login diagnostics contain only known schema keys, types and counts."""
    if not isinstance(value, dict):
        return None
    result = {}
    components = _own_component_schema(value.get("own_component_schema"))
    if components is not None:
        result["own_component_schema"] = components
    stage = value.get("legacy_proof_stage")
    if isinstance(stage, str) and stage in {
        "fresh_own",
        "owned_link",
        "listing",
        "seller",
        "recheck",
    }:
        result["legacy_proof_stage"] = stage
    counts = value.get("legacy_proof_counts")
    if isinstance(counts, dict):
        result["legacy_proof_counts"] = {
            name: counts[name]
            for name in ("own_edit_links", "own_logout_links", "matching_containers")
            if type(counts.get(name)) is int and 0 <= counts[name] <= 100
        }
    flags = value.get("legacy_proof_flags")
    if isinstance(flags, dict):
        result["legacy_proof_flags"] = {
            name: flags[name]
            for name in (
                "edit_text_matches",
                "owned_link_proven",
                "listing_page_present",
                "recheck_proven",
            )
            if type(flags.get(name)) is bool
        }
    if isinstance(value.get("page_path"), str) and value.get("page_path") in {
        "/m-meine-anzeigen.html",
        "/portal/anzeigen",
        "/m-einstellungen.html",
        "/m-mein-konto.html",
    }:
        result["page_path"] = value["page_path"]
    for name in (
        "astro_present",
        "astro_valid_json",
        "next_present",
        "next_valid_json",
    ):
        if type(value.get(name)) is bool:
            result[name] = value[name]
    for name, maximum in {
        "current_user_marker_count": 100,
        "own_header_data_count": 100,
        "own_header_link_count": 100,
        "candidate_id_count": 2,
    }.items():
        count = value.get(name)
        if type(count) is int and 0 <= count <= maximum:
            result[name] = count
    current_user = value.get("next_current_user")
    if (
        isinstance(current_user, dict)
        and isinstance(current_user.get("type"), str)
        and current_user["type"]
        in {"object", "array", "string", "number", "null", "missing"}
    ):
        keys = current_user.get("keys")
        result["next_current_user"] = {
            "type": current_user["type"],
            "keys": [
                key
                for key in keys[:2]
                if isinstance(key, str) and key in {"id", "userId"}
            ]
            if isinstance(keys, list)
            else [],
        }
    scripts = value.get("json_scripts")
    if isinstance(scripts, list):
        result["json_scripts"] = [
            {"name": script["name"], "type": script["type"]}
            for script in scripts[:2]
            if isinstance(script, dict)
            and isinstance(script.get("name"), str)
            and script["name"] in {"astroSharedData", "__NEXT_DATA__"}
            and isinstance(script.get("type"), str)
            and script["type"]
            in {"application/json", "application/ld+json", "other", "missing"}
        ]
    sources = []
    raw_sources = value.get("sources")
    if isinstance(raw_sources, list):
        for source in raw_sources[:5]:
            if not isinstance(source, dict):
                continue
            name, kind = source.get("name"), source.get("type")
            if (
                not isinstance(name, str)
                or name
                not in {"user", "userProfile", "currentUser", "account", "profile"}
                or not isinstance(kind, str)
                or kind
                not in {"object", "array", "string", "number", "null", "missing"}
            ):
                continue
            keys = source.get("keys")
            sources.append(
                {
                    "name": name,
                    "type": kind,
                    "keys": [
                        key
                        for key in keys[:8]
                        if isinstance(key, str)
                        and key
                        in {
                            "id",
                            "userId",
                            "user",
                            "currentUser",
                            "profile",
                            "account",
                            "email",
                            "name",
                        }
                    ]
                    if isinstance(keys, list)
                    else [],
                }
            )
    result["sources"] = sources
    return result


def _private_read(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise _error(
            "unsafe_safari_storage", "Die Safari-Konfiguration ist nicht sicher lesbar."
        ) from None
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise _error(
                "unsafe_safari_storage",
                "Die Safari-Konfiguration ist nicht sicher lesbar.",
            )
        os.fchmod(handle.fileno(), 0o600)
        value = handle.read(64 * 1024 + 1)
        if len(value) > 64 * 1024:
            raise _error(
                "unsafe_safari_storage", "Die Safari-Konfiguration ist ungültig."
            )
        return value


def _private_write(path, value):
    if path.is_symlink():
        raise _error(
            "unsafe_safari_storage", "Die Safari-Konfiguration darf kein Link sein."
        )
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    descriptor = os.open(
        temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class _Command:
    wire: dict
    profile_id: str
    future: asyncio.Future
    delivered: bool = False
    image: dict | None = None
    transfers: dict = field(default_factory=dict)
    on_image_preview: object = None
    preview: dict | None = None
    preview_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SafariBridge:
    def __init__(self, root, *, command_timeout=45, connect_timeout=1800):
        self.root = Path(root)
        if self.root.is_symlink():
            raise _error(
                "unsafe_safari_storage",
                "Das Safari-Datenverzeichnis darf kein Link sein.",
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.command_timeout = command_timeout
        self.connect_timeout = connect_timeout
        token_path = self.root / "safari-token"
        raw = _private_read(token_path)
        if raw is None:
            raw = secrets.token_urlsafe(32).encode("ascii")
            # Creation is exclusive so competing initialisers cannot rotate a
            # token that the native application has already imported.
            try:
                fd = os.open(
                    token_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                raw = _private_read(token_path)
        try:
            self._token = raw.decode("ascii")
        except (AttributeError, UnicodeDecodeError):
            raise _error(
                "unsafe_safari_storage", "Der Safari-Verbindungsschlüssel ist ungültig."
            ) from None
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", self._token):
            raise _error(
                "unsafe_safari_storage", "Der Safari-Verbindungsschlüssel ist ungültig."
            )
        raw = _private_read(self.root / "safari-bindings.json")
        try:
            self._bindings = json.loads(raw) if raw else {}
            if not isinstance(self._bindings, dict):
                raise ValueError
            for account_id, binding in self._bindings.items():
                validate_id(account_id, account=True)
                _profile(binding["profile_id"])
                _user(binding["user_id"])
                if type(binding["tab_id"]) is not int or binding["tab_id"] < 0:
                    raise ValueError
        except (ValueError, TypeError, KeyError):
            raise _error(
                "unsafe_safari_storage", "Die Safari-Accountzuordnung ist ungültig."
            ) from None
        self._commands = {}
        self._queues = defaultdict(deque)
        self._pending = {}
        self._closed = False
        self._last_native_poll_at = None
        self._profile_last_poll_at = {}
        self._profile_image_preview_capable = {}
        self._observed_profiles = set()
        self._exchange_counts = defaultdict(int)
        self._error_counts = defaultdict(int)

    def pairing_config(self, endpoint):
        return {"version": 1, "endpoint": endpoint, "token": self._token}

    def authenticate(self, token):
        return isinstance(token, str) and hmac.compare_digest(token, self._token)

    def _save_bindings(self):
        _private_write(
            self.root / "safari-bindings.json", json.dumps(self._bindings).encode()
        )

    def binding(self, account):
        binding = self._bindings.get(account["account_id"])
        if not binding or binding["user_id"] != account.get("user_id"):
            raise _error(
                "reauth_required",
                "Diesen Account in seinem Safari-Profil erneut verbinden.",
            )
        return binding

    def restore_tab(self, account, previous, identity):
        """Replace only a stale tab handle after a profile-scoped identity proof."""
        account_id = account["account_id"]
        if self._closed or self._bindings.get(account_id) != previous:
            raise _error(
                "safari_binding_changed",
                "Die Safari-Zuordnung hat sich während der Wiederverbindung geändert.",
            )
        user_id = _user(identity.get("user_id"))
        if (
            previous["user_id"] != account.get("user_id")
            or user_id != previous["user_id"]
            or identity.get("account_user_id") != user_id
        ):
            raise _error(
                "account_mismatch",
                "Das Safari-Profil ist mit einem anderen Konto angemeldet.",
            )
        tab_id = identity.get("recovered_tab_id")
        if type(tab_id) is not int or tab_id < 0 or identity.get(
            "identity_page_path"
        ) not in {"/m-meine-anzeigen.html", "/portal/anzeigen"}:
            raise _error(
                "invalid_safari_result", "Die Safari-Tabprüfung ist ungültig.", 422
            )
        bindings = {
            **self._bindings,
            account_id: {**previous, "tab_id": tab_id},
        }
        # Keep memory and disk unchanged if the durable write fails. There is no
        # await between the compare, private atomic write and in-memory commit.
        _private_write(
            self.root / "safari-bindings.json", json.dumps(bindings).encode()
        )
        self._bindings = bindings
        return {
            key: value
            for key, value in identity.items()
            if key not in {"recovered_tab_id", "account_user_id"}
        }

    async def connect(self, account, identify):
        account_id = validate_id(account["account_id"], account=True)
        if self._closed:
            raise _error(
                "safari_unavailable", "Die Safari-Verbindung wurde beendet.", 503
            )
        if account_id in self._pending:
            raise _error(
                "connect_in_progress", "Die Safari-Verbindung wird bereits hergestellt."
            )
        future = asyncio.get_running_loop().create_future()
        self._pending[account_id] = {
            "label": account.get("label", account_id),
            "future": future,
        }
        try:
            try:
                candidate = await asyncio.wait_for(future, self.connect_timeout)
            except TimeoutError:
                raise _error(
                    "safari_pairing_timeout",
                    "Die manuelle Safari-Verknüpfung wurde nicht abgeschlossen. connect_account erneut aufrufen und im gewünschten Safari-Profil „Diesen Tab verbinden“ wählen; eine vorhandene Kontoanmeldung bleibt gültig.",
                ) from None
            identity = await identify(candidate)
            user_id = _user(identity.get("user_id"))
            if account.get("user_id") and account["user_id"] != user_id:
                raise _error(
                    "account_mismatch",
                    "Das Safari-Profil ist mit einem anderen Konto angemeldet.",
                )
            for other_id, other in self._bindings.items():
                if other_id != account_id and (
                    other["profile_id"] == candidate["profile_id"]
                    or other["user_id"] == user_id
                ):
                    raise _error(
                        "account_already_bound",
                        "Safari-Profil oder Konto ist bereits einem anderen Account zugeordnet.",
                    )
            self._bindings[account_id] = {**candidate, "user_id": user_id}
            self._save_bindings()
            return identity
        finally:
            self._pending.pop(account_id, None)

    async def disconnect(self, account):
        account_id = account["account_id"]
        for command in list(self._commands.values()):
            if command.wire["account_id"] == account_id and not command.future.done():
                command.future.set_exception(
                    _error("safari_disconnected", "Der Safari-Account wurde getrennt.")
                )
        self._bindings.pop(account_id, None)
        self._save_bindings()

    async def close(self):
        self._closed = True
        for pending in self._pending.values():
            if not pending["future"].done():
                pending["future"].cancel()
        for command in self._commands.values():
            if not command.future.done():
                command.future.cancel()
        self._commands.clear()
        self._queues.clear()

    async def request(
        self,
        account,
        method,
        args,
        *,
        candidate=None,
        image=None,
        before_dispatch=None,
        on_image_preview=None,
    ):
        if method not in _METHODS:
            raise _error("invalid_safari_action", "Unbekannter Safari-Auftrag.", 422)
        if self._closed:
            raise _error(
                "safari_unavailable", "Die Safari-Verbindung wurde beendet.", 503
            )
        binding = candidate or self.binding(account)
        command_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        wire = {
            "id": command_id,
            "account_id": account["account_id"],
            "user_id": account.get("user_id"),
            "method": method,
            "args": {**args, "tab_id": binding["tab_id"]},
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=self.command_timeout)
            ).isoformat(),
        }
        # Reject oversized commands before journalling dispatch or consuming
        # their sole delivery. The native host also bounds replies to 64 KiB.
        if len(json.dumps(wire, ensure_ascii=False, allow_nan=False).encode()) > 60_000:
            raise _error(
                "response_too_large",
                "Der Safari-Auftrag überschreitet das Übertragungslimit.",
                413,
            )
        if before_dispatch is not None:
            await before_dispatch()
        command = _Command(
            wire, binding["profile_id"], future, image=image,
            on_image_preview=on_image_preview,
        )
        self._commands[command_id] = command
        self._queues[binding["profile_id"]].append(command_id)
        try:
            return await asyncio.wait_for(future, self.command_timeout)
        except TimeoutError:
            raise _error(
                "safari_timeout",
                "Safari hat den Auftrag nicht rechtzeitig bestätigt.",
                504,
            ) from None
        finally:
            self._commands.pop(command_id, None)
            queue = self._queues[binding["profile_id"]]
            try:
                queue.remove(command_id)
            except ValueError:
                pass
            # Never requeue a claimed command, even when the HTTP poll response
            # or the result was lost. Late results cannot resurrect operations.
            command.transfers.clear()
            command.image = None
            command.on_image_preview = None

    def _active_command(self, body, profile_id):
        command = self._commands.get(body.get("command_id"))
        if command is None or command.future.done() or not command.delivered:
            raise _error(
                "safari_command_gone", "Der Safari-Auftrag ist nicht mehr aktiv."
            )
        if command.profile_id != profile_id:
            raise _error(
                "safari_profile_mismatch",
                "Der Auftrag gehört zu einem anderen Safari-Profil.",
                403,
            )
        return command

    async def status(self):
        """Local diagnostic counters only; no tokens, page data or messages."""
        commands = list(self._commands.values())
        return {
            "enabled": not self._closed,
            "last_native_poll_at": self._last_native_poll_at,
            "profile_count": len(self._observed_profiles),
            "bound_account_count": len(self._bindings),
            "accounts": [
                {
                    "account_id": account_id,
                    "last_native_poll_at": self._profile_last_poll_at.get(
                        binding["profile_id"]
                    ),
                    "image_preview_capable": self._profile_image_preview_capable.get(
                        binding["profile_id"], False
                    ),
                    "command_counts": {
                        "queued": sum(
                            command.wire["account_id"] == account_id
                            and not command.delivered
                            for command in commands
                        ),
                        "delivered": sum(
                            command.wire["account_id"] == account_id
                            and command.delivered
                            for command in commands
                        ),
                    },
                }
                for account_id, binding in sorted(self._bindings.items())
            ],
            "pending_account_ids": sorted(self._pending),
            "awaiting_pair_account_ids": sorted(
                key
                for key, value in self._pending.items()
                if not value["future"].done()
            ),
            "command_counts": {
                "queued": sum(not command.delivered for command in commands),
                "delivered": sum(command.delivered for command in commands),
            },
            "exchange_counts": dict(self._exchange_counts),
            "error_counts": dict(self._error_counts),
        }

    async def exchange(self, body, *, native_image_preview_capable=False):
        action = body.get("action") if isinstance(body, dict) else None
        safe_action = (
            action
            if isinstance(action, str)
            and action
            in {"poll", "pair", "result", "image_chunk", "archive_image_chunk", "image_preview"}
            else "invalid"
        )
        self._exchange_counts[safe_action] += 1
        try:
            return await self._exchange(
                body, native_image_preview_capable=native_image_preview_capable
            )
        except MessagingError as exc:
            self._error_counts[exc.code] += 1
            raise

    async def _exchange(self, body, *, native_image_preview_capable=False):
        if (
            not isinstance(body, dict)
            or type(body.get("version")) is not int
            or body.get("version") != 1
        ):
            raise _error("invalid_safari_request", "Ungültiger Safari-Auftrag.", 422)
        if self._closed:
            raise _error(
                "safari_unavailable", "Die Safari-Verbindung wurde beendet.", 503
            )
        profile_id = _profile(body.get("profile_id"))
        action = body.get("action")
        if not isinstance(action, str):
            raise _error("invalid_safari_action", "Unbekannter Safari-Auftrag.", 422)
        if action == "poll":
            self._last_native_poll_at = datetime.now(timezone.utc).isoformat()
            if len(self._observed_profiles) < 100:
                self._observed_profiles.add(profile_id)
            if (
                profile_id in self._profile_last_poll_at
                or len(self._profile_last_poll_at) < 100
                or any(
                    binding["profile_id"] == profile_id
                    for binding in self._bindings.values()
                )
            ):
                self._profile_last_poll_at[profile_id] = self._last_native_poll_at
                # The authenticated HTTP layer supplies this fixed native-host
                # header separately. A JS/body field is never capability proof.
                self._profile_image_preview_capable[profile_id] = (
                    native_image_preview_capable is True
                )
            queue = self._queues[profile_id]
            command = None
            while queue:
                candidate = self._commands.get(queue.popleft())
                if (
                    candidate
                    and not candidate.future.done()
                    and not candidate.delivered
                ):
                    candidate.delivered = True
                    command = candidate.wire
                    break
            return {
                "command": command,
                "pending_accounts": [
                    {"account_id": key, "label": value["label"]}
                    for key, value in list(self._pending.items())[:50]
                    if not value["future"].done()
                ]
                if command is None
                else [],
            }
        if action == "pair":
            account_id = validate_id(body.get("account_id"), account=True)
            pending = self._pending.get(account_id)
            tab_id = body.get("tab_id")
            if type(tab_id) is not int or tab_id < 0:
                raise _error("invalid_safari_tab", "Ungültiger Safari-Tab.", 422)
            if not pending or pending["future"].done():
                raise _error(
                    "safari_pairing_not_pending",
                    "Für diesen Account wartet keine Safari-Verbindung.",
                )
            if any(
                key != account_id and value["profile_id"] == profile_id
                for key, value in self._bindings.items()
            ):
                raise _error(
                    "safari_profile_in_use",
                    "Für den zweiten Account ist ein getrenntes Safari-Profil erforderlich.",
                )
            pending["future"].set_result({"profile_id": profile_id, "tab_id": tab_id})
            return {"accepted": True, "identity_pending": True}
        if action not in {"result", "image_chunk", "archive_image_chunk", "image_preview"}:
            raise _error("invalid_safari_action", "Unbekannter Safari-Auftrag.", 422)
        command = self._active_command(body, profile_id)
        if action == "image_chunk":
            return self._image_chunk(command, body)
        if action == "archive_image_chunk":
            return self._archive_image_chunk(command, body)
        if action == "image_preview":
            return await self._image_preview(command, body, profile_id)
        error = body.get("error")
        result = body.get("result")
        if isinstance(error, dict) and result is None:
            code = error.get("code")
            if code == "access_blocked":
                code = "upstream_blocked"
            if not isinstance(code, str) or code not in _ERRORS:
                code = "ui_unsupported"
            self._error_counts[code] += 1
            diagnostic = _identity_structure(error.get("identity_structure"))
            details = (
                {"identity_structure": diagnostic} if diagnostic is not None else {}
            )
            reader = _reader_structure(error.get("reader_structure"))
            if reader is not None:
                details["reader_structure"] = reader
            image_diagnostic = image_structure(error.get("image_structure"))
            if image_diagnostic is not None:
                details["image_structure"] = image_diagnostic
            revision = _content_revision(error.get("content_revision"))
            if revision is not None:
                details["content_revision"] = revision
            navigation = error.get("navigation_state")
            if code == "safari_navigation_timeout" and isinstance(navigation, dict):
                details["navigation_state"] = {
                    name: navigation[name]
                    for name in (
                        "tab_complete",
                        "target_matched",
                        "marker_received",
                        "marker_ready",
                        "fresh_document",
                    )
                    if type(navigation.get(name)) is bool
                }
            marker_delivery = error.get("marker_delivery")
            if (
                code == "safari_navigation_timeout"
                and isinstance(marker_delivery, str)
                and marker_delivery
                in {
                    "received",
                    "no_reply",
                    "send_rejected",
                    "handler_error",
                    "sender_rejected",
                    "invalid_reply",
                }
            ):
                details["marker_delivery"] = marker_delivery
            bootstrap = error.get("content_bootstrap")
            if code == "safari_navigation_timeout" and isinstance(bootstrap, dict):
                delivery, stage = bootstrap.get("delivery"), bootstrap.get("stage")
                flags = ("protocol_ready", "secure_random_ready", "handler_ready")
                if (
                    isinstance(delivery, str)
                    and delivery
                    in {
                        "received",
                        "no_reply",
                        "send_rejected",
                        "sender_rejected",
                        "invalid_reply",
                    }
                    and isinstance(stage, str)
                    and stage
                    in {
                        "bootstrap",
                        "content_started",
                        "initialized",
                        "handler_ready",
                        "unavailable",
                    }
                    and all(type(bootstrap.get(name)) is bool for name in flags)
                ):
                    details["content_bootstrap"] = {
                        "delivery": delivery,
                        "stage": stage,
                        **{name: bootstrap[name] for name in flags},
                    }
            command.future.set_exception(
                MessagingError(
                    code,
                    "Der verknüpfte Safari-Tab ist nicht mehr geöffnet. Den Account mit connect_account erneut mit seinem aktuellen Tab verbinden."
                    if code == "safari_tab_missing"
                    else "Die Safari-Nachrichtenoberfläche hat den Auftrag nicht bestätigt.",
                    409,
                    details,
                )
            )
        elif isinstance(result, dict) and error is None:
            if command.wire["method"] == "get_conversation":
                self._resolve_transfers(command, result)
            command.future.set_result(result)
        else:
            raise _error("invalid_safari_result", "Ungültiges Safari-Ergebnis.", 422)
        return {"accepted": True}

    async def _image_preview(self, command, body, profile_id):
        allowed = {"version", "profile_id", "action", "command_id", "preview_sha256"}
        if (
            set(body) not in (allowed, allowed | {"image_pixels"})
            or command.wire["method"] != "send_step"
            or command.wire["args"].get("kind") != "image"
            or command.image is None
            or not callable(command.on_image_preview)
        ):
            raise _error("image_unavailable", "Für diesen Auftrag ist keine Bildvorschau freigegeben.", 403)
        try:
            preview = image_preview({
                key: body[key] for key in ("preview_sha256", "image_pixels") if key in body
            })
        except ValueError:
            raise _error("image_unavailable", "Die Bildvorschau enthält keinen gültigen exakten Beleg.", 422) from None
        async with command.preview_lock:
            self._active_command(body, profile_id)
            if command.preview is not None:
                if command.preview != preview:
                    raise _error("outcome_unknown", "Die Bildvorschau hat sich während des Auftrags verändert.")
            else:
                # ACK only after the account's operation journal has committed.
                # If this fails or expires, the content script must never click.
                await command.on_image_preview(preview)
                self._active_command(body, profile_id)
                command.preview = preview
        return {"accepted": True}

    def _image_chunk(self, command, body):
        image = command.image
        offset = body.get("offset")
        if command.wire["method"] != "send_step" or image is None:
            raise _error(
                "image_unavailable",
                "Für diesen Auftrag ist kein Bild freigegeben.",
                403,
            )
        data = image["data"]
        if (
            type(offset) is not int
            or offset < 0
            or offset >= len(data)
            or offset % CHUNK_BYTES
        ):
            raise _error("invalid_image_offset", "Ungültiger Bildabschnitt.", 422)
        return {
            "data_base64": base64.b64encode(
                data[offset : offset + CHUNK_BYTES]
            ).decode(),
            "total_bytes": len(data),
            "offset": offset,
            "sha256": image["sha256"],
            "mime_type": image["mime_type"],
            "filename": image["filename"],
        }

    def _archive_image_chunk(self, command, body):
        if command.wire["method"] != "get_conversation":
            raise _error(
                "image_unavailable",
                "Dieser Auftrag liest keine Nachrichtenbilder.",
                403,
            )
        transfer_id = validate_id(body.get("transfer_id"))
        total = body.get("total_bytes")
        offset = body.get("offset")
        digest = body.get("sha256")
        if (
            type(total) is not int
            or not 0 < total <= MAX_IMAGE_BYTES
            or type(offset) is not int
            or offset < 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise _error("invalid_image_transfer", "Ungültige Bildübertragung.", 422)
        try:
            encoded = body.get("data_base64")
            if (
                not isinstance(encoded, str)
                or len(encoded) > (CHUNK_BYTES + 2) // 3 * 4
            ):
                raise ValueError
            chunk = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise _error(
                "invalid_image_transfer", "Ungültiger Bildabschnitt.", 422
            ) from None
        if not chunk or len(chunk) > CHUNK_BYTES:
            raise _error("invalid_image_transfer", "Ungültiger Bildabschnitt.", 422)
        transfer = command.transfers.get(transfer_id)
        if transfer is None:
            if (
                offset
                or len(command.transfers) >= 20
                or sum(x["total"] for x in command.transfers.values()) + total
                > MAX_TRANSFER_BYTES
            ):
                raise _error(
                    "image_transfer_limit",
                    "Die Bildübertragung überschreitet das Auftragslimit.",
                    413,
                )
            transfer = command.transfers[transfer_id] = {
                "total": total,
                "sha256": digest,
                "data": bytearray(),
            }
        if (
            transfer["total"] != total
            or transfer["sha256"] != digest
            or offset != len(transfer["data"])
            or offset + len(chunk) > total
        ):
            raise _error(
                "invalid_image_transfer",
                "Bildabschnitte sind unvollständig oder widersprüchlich.",
                422,
            )
        transfer["data"].extend(chunk)
        return {"accepted": True, "next_offset": len(transfer["data"])}

    def _resolve_transfers(self, command, result):
        messages = result.get("messages")
        if not isinstance(messages, list) or len(messages) > 200:
            raise _error(
                "invalid_safari_result", "Nachrichten fehlen im Safari-Ergebnis.", 422
            )
        resolved = {}
        for message in messages:
            if not isinstance(message, dict) or not isinstance(
                message.get("images", []), list
            ):
                raise _error(
                    "invalid_safari_result", "Ungültige Safari-Nachricht.", 422
                )
            for image in message.get("images", []):
                if not isinstance(image, dict) or "data" in image or "path" in image:
                    raise _error(
                        "invalid_safari_result",
                        "Ungültiges Safari-Nachrichtenbild.",
                        422,
                    )
                image_error = image.pop("error", None)
                diagnostic = image_structure(
                    image_error.get("image_structure")
                    if isinstance(image_error, dict)
                    else None
                )
                transfer_id = image.pop("transfer_id", None)
                transfer = command.transfers.get(transfer_id)
                if (
                    transfer
                    and len(transfer["data"]) == transfer["total"]
                    and hashlib.sha256(transfer["data"]).hexdigest()
                    == transfer["sha256"]
                ):
                    if transfer_id not in resolved:
                        resolved[transfer_id] = bytes(transfer["data"])
                    image["data"] = resolved[transfer_id]
                else:
                    image["unavailable"] = True
                    image["error"] = {"code": "image_unavailable"}
                    if diagnostic is not None:
                        image["error"]["image_structure"] = diagnostic


class SafariMessagingAdapter:
    supports_conversation_observation = True
    supports_image_preparation = True

    def __init__(self, root, controller, bridge):
        self.root = Path(root)
        self.controller = controller
        self.bridge = bridge
        self._locks = defaultdict(asyncio.Lock)

    async def _call(
        self, account, method, args, *, candidate=None, image=None, before_dispatch=None,
        on_image_preview=None,
    ):
        async def operation():
            if candidate is None:
                self.bridge.binding(account)
            try:
                result = await self.bridge.request(
                    account,
                    method,
                    args,
                    candidate=candidate,
                    image=image,
                    before_dispatch=before_dispatch,
                    on_image_preview=on_image_preview,
                )
            except MessagingError as exc:
                if exc.code in {"upstream_blocked", "upstream_rate_limited"}:
                    await self.controller.report_interaction_block(exc.code)
                raise
            if method != "identify" and result.get("account_user_id") != account.get(
                "user_id"
            ):
                raise _error(
                    "account_mismatch",
                    "Das Safari-Ergebnis gehört nicht zum ausgewählten Konto.",
                )
            context = result.get("provider_context")
            signal = context.get("upstream_error") if isinstance(context, dict) else None
            if method == "get_conversation" and isinstance(signal, str) and signal in {
                "upstream_blocked", "upstream_rate_limited"
            }:
                # Preserve already fetched chat data while applying the same
                # circuit restriction as an ordinary blocked interaction.
                result["_upstream_reached"] = False
                result["access_warning"] = {
                    "code": signal,
                    "message": "Die Anbieterabfrage wurde eingeschränkt. Bereits gelesene Chatdaten bleiben archiviert; weitere Live-Abrufe müssen warten.",
                }
                try:
                    await self.controller.report_interaction_block(signal)
                except (UpstreamBlocked, UpstreamRateLimited):
                    pass
            return result

        result = await self.controller.run_interaction(
            operation,
            timeout_seconds=self.bridge.command_timeout + 5,
            upstream_verified=lambda value: value.get("_upstream_reached") is True,
        )
        result.pop("_upstream_reached", None)
        return result

    async def connect(self, account):
        async with self._locks[account["account_id"]]:
            if account.get("user_id"):
                try:
                    previous = dict(self.bridge.binding(account))
                except MessagingError as exc:
                    if exc.code != "reauth_required":
                        raise
                else:
                    try:
                        identity = await self._call(
                            account, "recover_tab", {}, candidate=previous
                        )
                    except MessagingError as exc:
                        if exc.code not in {
                            "safari_tab_missing",
                            "safari_tab_ambiguous",
                            "safari_recovery_unavailable",
                            "reauth_required",
                        }:
                            raise
                    else:
                        return self.bridge.restore_tab(account, previous, identity)
            return await self.bridge.connect(
                account,
                lambda candidate: self._call(
                    account, "identify", {}, candidate=candidate
                ),
            )

    async def disconnect(self, account):
        async with self._locks[account["account_id"]]:
            await self.bridge.disconnect(account)

    async def close(self):
        await self.bridge.close()

    async def list_conversations(
        self, account, cursor=None, limit=50, unread_only=False
    ):
        async with self._locks[account["account_id"]]:
            result = await self._call(
                account,
                "list_conversations",
                {"cursor": cursor, "limit": limit, "unread_only": unread_only},
            )
            if (
                not isinstance(result.get("conversations"), list)
                or len(result["conversations"]) > limit
            ):
                raise _error(
                    "ui_unsupported",
                    "Die Safari-Gesprächsliste ist nicht eindeutig lesbar.",
                )
            for conversation in result["conversations"]:
                validate_id(conversation.get("conversation_id"))
            return result

    async def get_conversation(self, account, conversation_id, cursor=None, limit=50):
        async with self._locks[account["account_id"]]:
            validate_id(conversation_id)
            result = await self._call(
                account,
                "get_conversation",
                {"conversation_id": conversation_id, "cursor": cursor, "limit": limit},
            )
            if (
                result.get("conversation_id") != conversation_id
                or not isinstance(result.get("messages"), list)
                or len(result["messages"]) > limit
            ):
                raise _error(
                    "conversation_mismatch",
                    "Das Safari-Gespräch stimmt nicht mit dem Auftrag überein.",
                )
            _user(result.get("recipient_user_id"))
            for message in result["messages"]:
                validate_id(message.get("message_id"))
                if message.get("sender_user_id") not in {
                    account["user_id"],
                    result["recipient_user_id"],
                }:
                    raise _error(
                        "recipient_unverified",
                        "Der Nachrichtenabsender ist nicht eindeutig.",
                    )
            return result

    async def prepare_send(self, account, target_kind, target_id):
        async with self._locks[account["account_id"]]:
            return await self._call(
                account,
                "prepare_send",
                {"target_kind": target_kind, "target_id": target_id},
            )

    async def inspect_ui(self, account, *, view="current", listing_id=None):
        """Read only bounded structural diagnostics, never message bodies."""
        if view not in {"current", "inbox", "listing"} or (view == "listing") != (
            listing_id is not None
        ):
            raise _error(
                "invalid_inspection_target",
                "Für diese Diagnoseansicht ist genau eine passende Anzeigen-ID erforderlich.",
                422,
            )
        args = {"view": view}
        if listing_id is not None:
            try:
                args.update(
                    target_kind="listing", target_id=normalize_listing_id(listing_id)
                )
            except (ValueError, TypeError):
                raise _error(
                    "invalid_listing_id", "Ungültige Anzeigen-ID.", 422
                ) from None
        async with self._locks[account["account_id"]]:
            result = await self._call(account, "inspect_ui", args)
            return _sanitize_structure(result)

    async def send_step(
        self,
        account,
        target,
        kind,
        *,
        text=None,
        image=None,
        before_dispatch,
        on_conversation_observed=None,
        on_image_prepared=None,
    ):
        async with self._locks[account["account_id"]]:
            wire_image = transfer = None
            if kind == "image":
                if (
                    not target.get("capabilities", {}).get("can_send_images")
                    or image is None
                ):
                    raise _error(
                        "images_unsupported",
                        "Der Bildversand wurde für diese Safari-Oberfläche noch nicht nachgewiesen.",
                        422,
                    )

                def read_image():
                    try:
                        with Path(image["path"]).open("rb") as handle:
                            return handle.read(MAX_IMAGE_BYTES + 1)
                    except OSError:
                        raise _error(
                            "image_unavailable",
                            "Das archivierte Bild ist nicht verfügbar.",
                        ) from None

                data = await asyncio.to_thread(read_image)
                if (
                    not 0 < len(data) <= MAX_IMAGE_BYTES
                    or hashlib.sha256(data).hexdigest() != image["sha256"]
                ):
                    raise _error(
                        "image_changed", "Der archivierte Bildinhalt hat sich geändert."
                    )
                extension = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/webp": ".webp",
                }.get(image["mime_type"])
                if extension is None:
                    raise _error(
                        "images_unsupported", "Dieses Bildformat ist nicht unterstützt."
                    )
                wire_image = {
                    "image_id": image["image_id"],
                    "sha256": image["sha256"],
                    "mime_type": image["mime_type"],
                    "filename": image["image_id"] + extension,
                    "size_bytes": len(data),
                }
                transfer = {**wire_image, "data": data}
            if kind not in {"image", "text"}:
                raise _error("invalid_step", "Unbekannter Versandschritt.", 422)
            dispatch_started_at = None
            prepared_preview = None

            async def record_dispatch():
                nonlocal dispatch_started_at
                await before_dispatch()
                dispatch_started_at = datetime.now(timezone.utc)

            async def record_preview(value):
                nonlocal prepared_preview
                if not callable(on_image_prepared):
                    raise _error("image_unavailable", "Die Bildvorschau kann nicht dauerhaft bestätigt werden.")
                preview = image_preview(value)
                await on_image_prepared(preview)
                prepared_preview = preview

            result = await self._call(
                account,
                "send_step",
                {"target": target, "kind": kind, "text": text, "image": wire_image},
                image=transfer,
                before_dispatch=record_dispatch,
                on_image_preview=record_preview if kind == "image" else None,
            )
            if "_follow" not in result:
                if kind == "image":
                    raise _error(
                        "outcome_unknown",
                        "Ein Bildversand benötigt eine separate lesende Bildbestätigung.",
                    )
                return result
            if (
                not (
                    (kind == "text" and target.get("target_kind") == "listing")
                    or (
                        kind == "image"
                        and target.get("target_kind") == "conversation"
                        and target.get("conversation_id") == target.get("target_id")
                    )
                )
                or not callable(on_conversation_observed)
                or dispatch_started_at is None
            ):
                raise _error(
                    "outcome_unknown",
                    "Der beobachtete Gesprächslink bestätigt den Versand noch nicht.",
                )
            observation = self._follow_observation(
                result["_follow"],
                dispatch_started_at,
                kind=kind,
                expected_conversation_id=target.get("conversation_id"),
            )
            if kind == "image":
                followed = {
                    "preview_sha256": observation["image_sha256"],
                    **({"image_pixels": observation["image_pixels"]} if "image_pixels" in observation else {}),
                }
                if (
                    (callable(on_image_prepared) or "image_pixels" in observation)
                    and followed != prepared_preview
                ):
                    raise _error("outcome_unknown", "Die Bildbeobachtung stimmt nicht mit der vorher gespeicherten Vorschau überein.")
            # A navigation hint is not a receipt. Save it before requesting a
            # separate, rate-limited read so crash recovery can still inspect it.
            await on_conversation_observed(observation)
            confirmation = {
                "target": target,
                "conversation_id": observation["conversation_id"],
                "expected_message_id": observation["message_id"],
                "not_before": observation["not_before"],
                "text": text,
                "baseline_message_ids": [],
            }
            if kind == "image":
                confirmation.update(
                    kind="image",
                    text="",
                    expected_image_sha256=observation["image_sha256"],
                )
                if "image_pixels" in observation:
                    confirmation["expected_image_pixels"] = observation["image_pixels"]
            receipt = await self._call(
                account,
                "confirm_send",
                confirmation,
            )
            try:
                message_id = validate_id(receipt.get("message_id"))
                conversation_id = validate_id(receipt.get("conversation_id"))
            except MessagingError:
                raise _error(
                    "outcome_unknown",
                    "Die lesende Versandbestätigung ist nicht eindeutig.",
                ) from None
            if (
                receipt.get("confirmed") is not True
                or receipt.get("recipient_user_id") != target.get("recipient_user_id")
                or conversation_id != observation["conversation_id"]
                or (
                    observation["message_id"] is not None
                    and message_id != observation["message_id"]
                )
                or "_follow" in receipt
            ):
                raise _error(
                    "outcome_unknown",
                    "Die lesende Versandbestätigung gehört nicht eindeutig zum Versandauftrag.",
                )
            confirmed = {
                "message_id": message_id,
                "conversation_id": conversation_id,
                "recipient_user_id": receipt["recipient_user_id"],
                "created_at": receipt.get("created_at"),
            }
            if kind == "image":
                try:
                    created_at = receipt.get("created_at")
                    if (
                        not isinstance(created_at, str)
                        or len(created_at) > 64
                        or re.fullmatch(
                            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
                            r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
                            created_at,
                        )
                        is None
                    ):
                        raise ValueError
                    receipt_time = datetime.fromisoformat(
                        created_at.replace("Z", "+00:00")
                    )
                    if (
                        receipt_time < datetime.fromisoformat(observation["not_before"])
                        or receipt_time > datetime.now(timezone.utc) + timedelta(seconds=5)
                    ):
                        raise ValueError
                except (ValueError, TypeError):
                    raise _error(
                        "outcome_unknown",
                        "Die Bildbestätigung enthält keinen passenden Versandzeitpunkt.",
                    ) from None
                try:
                    if receipt.get("kind") != "image":
                        raise ValueError
                    evidence = receipt_evidence(
                        receipt.get("image_evidence"), observation["image_sha256"],
                        observation.get("image_pixels"),
                    )
                except ValueError:
                    raise _error(
                        "outcome_unknown",
                        "Das gesendete Bild wurde im frischen Gespräch nicht eindeutig bestätigt.",
                    ) from None
                confirmed.update(kind="image", image_evidence=evidence)
            return confirmed

    @staticmethod
    def _follow_observation(
        value, dispatch_started_at, *, kind="text", expected_conversation_id=None
    ):
        try:
            if not isinstance(value, dict):
                raise ValueError
            conversation_id = validate_id(value.get("conversation_id"))
            message_id = value.get("message_id")
            if message_id is not None:
                validate_id(message_id)
            if kind == "image" and (
                set(value) - {"image_pixels"}
                != {
                    "kind",
                    "conversation_id",
                    "message_id",
                    "dispatched_at",
                    "image_sha256",
                }
                or value.get("kind") != "image"
                or message_id is None
                or conversation_id != expected_conversation_id
                or not isinstance(value.get("image_sha256"), str)
                or re.fullmatch(r"[a-f0-9]{64}", value["image_sha256"]) is None
            ):
                raise ValueError
            raw_time = value.get("dispatched_at")
            if not isinstance(raw_time, str) or len(raw_time) > 64:
                raise ValueError
            observed_at = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            if observed_at.tzinfo is None:
                raise ValueError
            observed_at = observed_at.astimezone(timezone.utc)
            if observed_at < dispatch_started_at - timedelta(
                seconds=5
            ) or observed_at > datetime.now(timezone.utc) + timedelta(seconds=5):
                raise ValueError
        except (ValueError, TypeError, MessagingError):
            raise _error(
                "outcome_unknown",
                "Der beobachtete Gesprächslink ist nicht eindeutig diesem Versand zuzuordnen.",
            ) from None
        observation = {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "not_before": max(dispatch_started_at, observed_at).isoformat(),
        }
        if kind == "image":
            observation.update(kind="image", image_sha256=value["image_sha256"])
            if "image_pixels" in value:
                try:
                    observation["image_pixels"] = pixel_proof(value["image_pixels"])
                except ValueError:
                    raise _error("outcome_unknown", "Der Pixelbeleg der Bildbeobachtung ist ungültig.") from None
        return observation
