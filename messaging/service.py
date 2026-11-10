"""Durable account-scoped messaging; uncertain dispatches are never retried."""

from __future__ import annotations

import asyncio
import base64
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import uuid

from utils.input_validation import normalize_listing_id
from utils.outbound_control import (
    OutboundCircuitOpen,
    UpstreamBlocked,
    UpstreamRateLimited,
)
from .contracts import (
    MAX_ATTACHMENTS,
    MAX_TEXT_LENGTH,
    MessagingError,
    MessagingSettings,
    validate_id,
)
from .store import MessagingStore, encode, now
from .diagnostics import image_structure
from .image_evidence import image_preview, pixel_proof, receipt_evidence

_ACCESS_BLOCK_ERRORS = (OutboundCircuitOpen, UpstreamBlocked, UpstreamRateLimited)


def _is_access_block(exc):
    return isinstance(exc, _ACCESS_BLOCK_ERRORS) or (
        isinstance(exc, MessagingError)
        and exc.code
        in {
            "upstream_blocked",
            "upstream_rate_limited",
            "outbound_circuit_open",
            "access_blocked",
        }
    )


def _public_image(image):
    return {k: v for k, v in image.items() if k not in {"path", "data"}}


def _sent_image_references(image, step):
    """The local upload is an original, not the platform's transformed image."""
    if image is None:
        return {"images": []}
    result = {
        "images": [],
        "upload_originals": [{**_public_image(image), "source": "upload_original"}],
    }
    if (
        isinstance(step.get("image_evidence"), dict)
        and step["image_evidence"].get("source") == "fresh_conversation_dom"
        and step.get("message_id")
        and step["message_id"] == step.get("observed_message_id")
    ):
        # Safari's fresh receipt proves exactly one attachment and its server
        # message ID. The reader uses this same stable attachment reference.
        result["images"] = [{
            "remote_id": step["message_id"] + ":0",
            "unavailable": True,
            "error": {
                "code": "image_unavailable",
                "message": "Dieses gesendete Bild wurde noch nicht live archiviert.",
            },
        }]
    return result


def _public_capabilities(value):
    """Preserve observed capabilities without turning untested forms into 'no'."""
    raw = value if isinstance(value, dict) else {}
    result = {"scope": "current_page"}
    for status_name, flag in (
        ("text_status", "can_send_text"),
        ("images_status", "can_send_images"),
        ("first_contact_images_status", "first_contact_images"),
    ):
        status = raw.get(status_name)
        boolean = raw.get(flag)
        if status == "supported" and boolean is True:
            result[status_name], result[flag] = status, True
        elif status == "unsupported" and boolean is False:
            result[status_name], result[flag] = status, False
        else:
            result[status_name], result[flag] = "unverified", None
    maximum = raw.get("max_text_length")
    source = raw.get("text_limit_source")
    if (
        result["can_send_text"] is True
        and type(maximum) is int
        and 0 < maximum <= MAX_TEXT_LENGTH
        and isinstance(source, str)
        and source in {"form", "application"}
    ):
        result.update(max_text_length=maximum, text_limit_source=source)
    formats = raw.get("accepted_image_types")
    if isinstance(formats, list):
        result["accepted_image_types"] = list(
            dict.fromkeys(
                token
                for token in formats[:32]
                if isinstance(token, str)
                and len(token) <= 80
                and re.fullmatch(
                    r"(?:image/(?:[a-zA-Z0-9.+_-]+|\*)|\.[a-zA-Z0-9]+)", token
                )
            )
        )
    if (
        result["can_send_images"] is True
        and isinstance(raw.get("image_send_mode"), str)
        and raw.get("image_send_mode")
        in {
            "auto_send",
            "explicit_submit",
        }
    ):
        result["image_send_mode"] = raw["image_send_mode"]
    image_maximum = raw.get("max_images_per_message")
    image_source = raw.get("image_limit_source")
    if (
        result["can_send_images"] is True
        and type(image_maximum) is int
        and 0 < image_maximum <= MAX_ATTACHMENTS
        and isinstance(image_source, str)
        and image_source in {"form", "website"}
    ):
        result.update(
            max_images_per_message=image_maximum,
            image_limit_source=image_source,
        )
    if any(
        result[name] == "unverified"
        for name in ("text_status", "images_status", "first_contact_images_status")
    ):
        result["limitation"] = (
            "Nicht verifizierte Funktionen müssen im jeweiligen Nachrichtenformular geprüft werden."
        )
    return result


def _public_operation(operation, *, replay=False):
    result = {k: v for k, v in operation.items() if k not in {"payload", "target"}}
    result["idempotent_replay"] = replay
    result["schema_version"] = 1
    state = operation["state"]
    result["next_action"] = {
        "waiting_for_login": "Im Browser beim gewünschten Account anmelden; bei Safari anschließend im Erweiterungsmenü „Diesen Tab verbinden“ wählen. Danach den Auftragsstatus abrufen.",
        "reauth_required": "connect_account für denselben Account aufrufen; nur noch nicht gesendete Schritte werden fortgesetzt.",
        "blocked": "Kleinanzeigen hat den Zugriff gesperrt. Keine automatischen Anmeldeversuche; erst nach Klärung der Sperre erneut anmelden.",
        "outcome_unknown": "Versandausgang unklar. Gespräch prüfen; keinen neuen Versandauftrag für denselben Inhalt erstellen.",
        "partial": "Ein Teil wurde gesendet. Schritte prüfen und nur nachweislich fehlende Inhalte separat beauftragen.",
        "queued": "get_message_operation mit derselben operation_id aufrufen.",
        "running": "get_message_operation mit derselben operation_id aufrufen.",
    }.get(state)
    error = operation.get("error")
    if (
        operation.get("kind") == "connect"
        and state == "reauth_required"
        and isinstance(error, dict)
        and isinstance(error.get("code"), str)
        and error.get("code")
        in {
            "safari_navigation_timeout",
            "safari_content_timeout",
            "safari_tab_missing",
            "safari_timeout",
            "safari_unavailable",
            "identity_unverified",
            "identity_page_required",
            "invalid_bridge_reply",
            "invalid_page_reply",
            "command_expired",
        }
    ):
        result["next_action"] = (
            "Safari-Erweiterung und lokale Verbindung prüfen. Die vorhandene "
            "Kontoanmeldung muss nicht wiederholt werden. Anschließend "
            "connect_account für denselben Account aufrufen."
        )
    if (
        operation.get("kind") == "connect"
        and state == "reauth_required"
        and isinstance(error, dict)
        and error.get("code") == "safari_pairing_timeout"
    ):
        result["next_action"] = (
            "connect_account für denselben Account erneut aufrufen und im gewünschten "
            "Safari-Profil im Erweiterungsmenü „Diesen Tab verbinden“ wählen. "
            "Die vorhandene Kontoanmeldung muss nicht wiederholt werden."
        )
    return result


class MessagingService:
    def __init__(self, settings: MessagingSettings, adapter, store=None):
        self.settings = settings
        self.adapter = adapter
        self.store = store or MessagingStore(settings.root)
        self._account_locks = defaultdict(asyncio.Lock)
        self._connect_request_locks = defaultdict(asyncio.Lock)
        self._identity_binding_lock = asyncio.Lock()
        self._tasks = set()
        self._connect_tasks = {}
        self._wake = asyncio.Event()
        self._worker = None
        self._closing = False
        self._storage_failed = False

    async def start(self):
        await self.store.start()
        try:
            # Dispatch may have reached Kleinanzeigen before the previous process died.
            for operation in await self.store.operations(
                {"running", "queued", "waiting_for_login"}
            ):
                if operation["kind"] == "connect":
                    operation["state"] = "reauth_required"
                    account = await self.store.account(operation["account_id"])
                    account["state"] = "reauth_required"
                    await self.store.save_account(account)
                elif any(s["state"] == "dispatching" for s in operation["steps"]):
                    operation["state"] = "outcome_unknown"
                    for step in operation["steps"]:
                        if step["state"] == "dispatching":
                            step["state"] = "outcome_unknown"
                elif operation["steps"] and all(
                    s["state"] == "sent" for s in operation["steps"]
                ):
                    operation["state"] = "sent"
                else:
                    operation["state"] = "queued"
                await self.store.save_operation(operation)
                if operation["kind"] == "send":
                    await self._archive_confirmed_steps(operation)
            self._worker = asyncio.create_task(
                self._run_worker(), name="messaging-dispatch"
            )
        except BaseException:
            await self.store.close()
            raise

    async def close(self):
        self._closing = True
        tasks = [*self._tasks]
        if self._worker:
            tasks.append(self._worker)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.adapter.close()
        finally:
            await self.store.close()

    async def list_accounts(self):
        return {
            "accounts": await self.store.accounts(),
            "test_policy": self.settings.public_policy(),
        }

    async def connect_account(self, account_id, label=None):
        validate_id(account_id, account=True)
        # Single-flight covers the async account/operation writes as well as
        # task registration; otherwise two simultaneous calls can both miss it.
        async with self._connect_request_locks[account_id]:
            return await self._begin_connect(account_id, label)

    async def _begin_connect(self, account_id, label):
        existing_task = self._connect_tasks.get(account_id)
        if existing_task is not None and not existing_task[1].done():
            return _public_operation(
                await self.store.operation(account_id, existing_task[0]), replay=True
            )
        try:
            account = await self.store.account(account_id)
        except MessagingError as exc:
            if exc.code != "account_not_found":
                raise
            account = {"account_id": account_id, "user_id": None, "created_at": now()}
        account.update(
            state="login_pending", label=(label or account.get("label") or account_id)
        )
        if not isinstance(account["label"], str) or len(account["label"]) > 128:
            raise MessagingError(
                "invalid_label",
                "Die Account-Bezeichnung darf höchstens 128 Zeichen enthalten.",
                422,
            )
        await self.store.save_account(account)
        operation = {
            "operation_id": str(uuid.uuid4()),
            "account_id": account_id,
            "kind": "connect",
            "state": "waiting_for_login",
            "steps": [],
            "created_at": now(),
        }
        await self.store.insert_operation(operation)
        task = asyncio.create_task(
            self._connect(account, operation), name="messaging-login"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._connect_tasks[account_id] = (operation["operation_id"], task)
        return _public_operation(operation)

    async def _connect(self, account, operation):
        async with self._account_locks[account["account_id"]]:
            try:
                identity = await self.adapter.connect(account)
                user_id = identity.get("user_id")
                if not user_id or (
                    account.get("user_id") and account["user_id"] != str(user_id)
                ):
                    raise MessagingError(
                        "account_mismatch",
                        "Das angemeldete Konto stimmt nicht mit dem gebundenen Account überein.",
                        409,
                    )
                validate_id(str(user_id))
                # Keep the cross-account uniqueness check and its durable write
                # under one lock, even while the SQLite store is contended.
                # Browser login itself remains independent for each account.
                async with self._identity_binding_lock:
                    account.update(await self.store.account(account["account_id"]))
                    if account.get("user_id") and account["user_id"] != str(user_id):
                        raise MessagingError(
                            "account_mismatch",
                            "Das angemeldete Konto stimmt nicht mit dem gebundenen Account überein.",
                            409,
                        )
                    for other in await self.store.accounts():
                        if other["account_id"] != account["account_id"] and other.get(
                            "user_id"
                        ) == str(user_id):
                            raise MessagingError(
                                "account_already_bound",
                                "Dieses Kleinanzeigen-Konto ist bereits einem anderen Account zugeordnet.",
                                409,
                            )
                    account.update(user_id=str(user_id), state="connected")
                    capabilities = _public_capabilities(identity.get("capabilities"))
                    account["capabilities"] = capabilities
                    if identity.get("display_name"):
                        account["display_name"] = identity["display_name"]
                    await self.store.save_account(account)
                operation["state"] = "connected"
                operation["user_id"] = str(user_id)
                operation["capabilities"] = capabilities
                await self.store.save_operation(operation)
                for pending in await self.store.operations({"reauth_required"}):
                    if (
                        pending["account_id"] == account["account_id"]
                        and pending["kind"] != "connect"
                    ):
                        pending["state"] = "queued"
                        pending.pop("error", None)
                        await self.store.save_operation(pending)
                self._wake.set()
            except (Exception, asyncio.CancelledError) as exc:
                state = "blocked" if _is_access_block(exc) else "reauth_required"
                account["state"] = state
                operation["state"] = state
                operation["error"] = self._error(exc)
                await self.store.save_account(account)
                await self.store.save_operation(operation)

    async def disconnect_account(self, account_id):
        account = await self.store.account(account_id)
        pending = self._connect_tasks.get(account_id)
        if pending and not pending[1].done():
            pending[1].cancel()
            await asyncio.gather(pending[1], return_exceptions=True)
        async with self._account_locks[account_id]:
            await self.adapter.disconnect(account)
            account["state"] = "disconnected"
            await self.store.save_account(account)
        return {
            "account_id": account_id,
            "state": "disconnected",
            "archive_preserved": True,
        }

    async def _connected(self, account_id):
        account = await self.store.account(account_id)
        if account.get("state") == "blocked":
            raise MessagingError(
                "access_blocked",
                "Kleinanzeigen hat den Zugriff gesperrt; keine automatischen Anmeldeversuche.",
                409,
            )
        if account.get("state") != "connected" or not account.get("user_id"):
            raise MessagingError(
                "reauth_required",
                "Bitte diesen Account im Anmeldefenster anmelden.",
                409,
            )
        return account

    async def _read(self, account_id, operation):
        async with self._account_locks[account_id]:
            account = await self._connected(account_id)
            try:
                return await operation(account)
            except MessagingError as exc:
                if exc.code == "reauth_required":
                    account["state"] = "reauth_required"
                    await self.store.save_account(account)
                raise

    async def inspect_safari_ui(self, account_id, *, view="current", listing_id=None):
        """Explicit local diagnostics share account locks and browser admission."""
        validate_id(account_id, account=True)
        inspector = getattr(self.adapter, "inspect_ui", None)
        if inspector is None:
            raise MessagingError(
                "safari_unavailable",
                "Die Safari-Oberflächendiagnose ist nicht eingerichtet.",
                409,
            )
        result = await self._read(
            account_id,
            lambda account: inspector(account, view=view, listing_id=listing_id),
        )
        return {
            **result,
            "account_id": account_id,
            "source": "live",
            "may_mark_read": view != "listing",
        }

    @staticmethod
    def _page(items, account_id, scope, cursor, limit):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 200
        ):
            raise MessagingError(
                "invalid_limit", "limit muss zwischen 1 und 200 liegen.", 422
            )
        offset = 0
        if cursor:
            try:
                body = json.loads(
                    base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
                )
                if body["account_id"] != account_id or body["scope"] != scope:
                    raise ValueError()
                offset = body["offset"]
                if type(offset) is not int or offset < 0:
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise MessagingError(
                    "invalid_cursor", "Cursor gehört nicht zu diesem Archivabruf.", 422
                ) from None
        selected = items[offset : offset + limit]
        has_more = offset + limit < len(items)
        next_cursor = (
            base64.urlsafe_b64encode(
                encode(
                    {"account_id": account_id, "scope": scope, "offset": offset + limit}
                ).encode()
            )
            .decode()
            .rstrip("=")
            if has_more
            else None
        )
        return selected, has_more, next_cursor

    async def list_conversations(
        self, account_id, cursor=None, limit=50, unread_only=False, source="live"
    ):
        await self.store.account(account_id)
        self._validate_read(source, limit)
        if source == "archive":
            items = await self.store.archived_conversations(account_id)
            items = [
                {**item, "capabilities": _public_capabilities(item["capabilities"])}
                if "capabilities" in item
                else item
                for item in items
            ]
            if unread_only:
                items = [item for item in items if item.get("unread")]
            items, more, next_cursor = self._page(
                items, account_id, "conversations:" + str(unread_only), cursor, limit
            )
            return {
                "account_id": account_id,
                "source": "archive",
                "conversations": items,
                "has_more": more,
                "next_cursor": next_cursor,
            }
        result = await self._read(
            account_id,
            lambda account: self.adapter.list_conversations(
                account, cursor=cursor, limit=limit, unread_only=unread_only
            ),
        )
        await self.store.archive_conversations(account_id, result["conversations"])
        return {
            **result,
            "account_id": account_id,
            "source": "live",
            "retrieved_at": now(),
        }

    async def get_conversation(
        self, account_id, conversation_id, cursor=None, limit=50, source="live"
    ):
        validate_id(conversation_id)
        await self.store.account(account_id)
        self._validate_read(source, limit)
        if source == "archive":
            meta, messages = await self.store.archived_conversation(
                account_id, conversation_id
            )
            if "capabilities" in meta:
                meta = {
                    **meta,
                    "capabilities": _public_capabilities(meta["capabilities"]),
                }
            messages, more, next_cursor = self._page(
                messages, account_id, "messages:" + conversation_id, cursor, limit
            )
            return {
                **meta,
                "account_id": account_id,
                "conversation_id": conversation_id,
                "source": "archive",
                "messages": messages,
                "has_more": more,
                "next_cursor": next_cursor,
            }
        result = await self._read(
            account_id,
            lambda account: self.adapter.get_conversation(
                account, conversation_id, cursor=cursor, limit=limit
            ),
        )
        if result.get("conversation_id") != conversation_id:
            raise MessagingError(
                "conversation_mismatch",
                "Die geladene Unterhaltung stimmt nicht mit dem Auftrag überein.",
                409,
            )
        return await self._archive_result(account_id, result)

    async def _archive_result(self, account_id, result):
        provider_observation = result.get("provider_context")
        system_events = result.get("system_events")
        result = {
            key: value for key, value in result.items()
            if key not in {"provider_context", "provider_context_error", "system_events", "system_events_error"}
        }
        if "capabilities" in result:
            result = {
                **result,
                "capabilities": _public_capabilities(result["capabilities"]),
            }
        media_error_codes = {
            "image_unavailable",
            "image_url_unavailable",
            "invalid_image",
            "invalid_image_size",
            "unsupported_image",
            "image_too_large",
        }

        def unavailable_image(item, code="image_unavailable"):
            result = {
                "unavailable": True,
                "remote_id": item.get("remote_id"),
                "error": {
                    "code": code
                    if isinstance(code, str) and code in media_error_codes
                    else "image_unavailable",
                    "message": "Dieses Bild konnte nicht geladen oder archiviert werden.",
                },
            }
            error = item.get("error")
            diagnostic = image_structure(
                error.get("image_structure") if isinstance(error, dict) else None
            )
            if diagnostic is not None:
                result["error"]["image_structure"] = diagnostic
            return result

        archived = []
        for message in result["messages"]:
            images = []
            for item in message.get("images", []):
                if "data" in item:
                    try:
                        image = await self.store.save_image(account_id, item["data"])
                    except MessagingError as exc:
                        if exc.code not in {
                            "invalid_image",
                            "invalid_image_size",
                            "unsupported_image",
                        }:
                            raise
                        images.append(unavailable_image(item, exc.code))
                        continue
                    images.append(
                        {**_public_image(image), "remote_id": item.get("remote_id")}
                    )
                elif item.get("image_id"):
                    images.append(
                        _public_image(
                            await self.store.image(account_id, item["image_id"])
                        )
                    )
                else:
                    error = item.get("error")
                    images.append(
                        unavailable_image(
                            item,
                            error.get("code")
                            if isinstance(error, dict)
                            else "image_unavailable",
                        )
                    )
            archived.append({**message, "images": images})
        meta = {
            k: v
            for k, v in result.items()
            if k not in {"messages", "next_cursor", "has_more"}
        }
        await self.store.archive_conversations(account_id, [meta])
        await self.store.archive_messages(
            account_id, result["conversation_id"], archived
        )
        if system_events is not None:
            try:
                result["system_events"] = await self.store.archive_system_events(
                    account_id, result["conversation_id"], system_events
                )
            except MessagingError as exc:
                if exc.code != "system_events_invalid":
                    raise
                result["system_events_error"] = exc.to_dict()
        if provider_observation is not None:
            try:
                await self.store.archive_provider_context(
                    account_id, result["conversation_id"], provider_observation
                )
            except MessagingError as exc:
                if exc.code != "provider_context_invalid":
                    raise
                # An unreadable or unbound public page must not discard a
                # successfully fetched chat, nor expose its untrusted content.
                result["provider_context_error"] = exc.to_dict()
        provider_context = await self.store.archived_provider_context(
            account_id, result["conversation_id"]
        )
        if provider_context is not None:
            result["provider_context"] = provider_context
        return {
            **result,
            "account_id": account_id,
            "messages": archived,
            "source": "live",
            "retrieved_at": now(),
        }

    @staticmethod
    def _validate_read(source, limit):
        if source not in {"live", "archive"}:
            raise MessagingError(
                "invalid_source", "source muss live oder archive sein.", 422
            )
        if type(limit) is not int or not 1 <= limit <= 200:
            raise MessagingError(
                "invalid_limit", "limit muss zwischen 1 und 200 liegen.", 422
            )

    async def upload_image(self, account_id, data, filename="image"):
        return _public_image(await self.store.save_image(account_id, data, filename))

    async def get_message_image(self, account_id, image_id):
        await self.store.account(account_id)
        image = await self.store.image(account_id, image_id)
        try:
            data = Path(image["path"]).read_bytes()
        except OSError:
            raise MessagingError(
                "image_unavailable",
                "Die archivierte Bilddatei ist nicht verfügbar.",
                404,
            ) from None
        if hashlib.sha256(data).hexdigest() != image["sha256"]:
            raise MessagingError(
                "image_changed", "Die archivierte Bilddatei wurde verändert.", 409
            )
        return {**_public_image(image), "data": data}

    async def submit_send(
        self,
        account_id,
        target_kind,
        target_id,
        text=None,
        attachment_ids=None,
        idempotency_key=None,
    ):
        if (
            self._closing
            or self._storage_failed
            or self._worker is None
            or self._worker.done()
        ):
            raise MessagingError(
                "messaging_unavailable",
                "Der Nachrichtenversand ist derzeit nicht betriebsbereit.",
                503,
            )
        await self.store.account(account_id)
        if target_kind not in {"listing", "conversation"}:
            raise MessagingError("invalid_target", "Ungültiges Nachrichtenziel.", 422)
        if target_kind == "listing":
            try:
                target_id = normalize_listing_id(target_id)
            except (ValueError, TypeError):
                raise MessagingError(
                    "invalid_listing_id", "Ungültige Anzeigen-ID.", 422
                ) from None
        else:
            validate_id(target_id)
        if not isinstance(idempotency_key, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", idempotency_key
        ):
            raise MessagingError(
                "invalid_idempotency_key",
                "Ein stabiler Idempotenzschlüssel ist erforderlich.",
                422,
            )
        if text is not None and (
            not isinstance(text, str) or len(text) > MAX_TEXT_LENGTH or not text.strip()
        ):
            raise MessagingError(
                "invalid_text",
                "Text muss nichtleer sein und darf höchstens 20.000 Zeichen enthalten.",
                422,
            )
        attachment_ids = attachment_ids or []
        if (
            not isinstance(attachment_ids, list)
            or len(attachment_ids) > MAX_ATTACHMENTS
            or len(set(attachment_ids)) != len(attachment_ids)
        ):
            raise MessagingError(
                "invalid_attachments", "Höchstens 20 Bilder pro Auftrag erlaubt.", 422
            )
        if not text and not attachment_ids:
            raise MessagingError(
                "empty_message", "Text oder mindestens ein Bild ist erforderlich.", 422
            )
        # First contact always establishes a thread through a text message.
        if target_kind == "listing" and not text:
            raise MessagingError(
                "first_message_requires_text",
                "Für den Erstkontakt ist ein Nachrichtentext erforderlich.",
                422,
            )
        images = [
            await self.store.image(account_id, image_id) for image_id in attachment_ids
        ]
        payload = {
            "target_kind": target_kind,
            "target_id": target_id,
            "text": text,
            "attachment_ids": attachment_ids,
            "image_hashes": [item["sha256"] for item in images],
        }
        fingerprint = hashlib.sha256(
            encode({"account_id": account_id, **payload}).encode()
        ).hexdigest()
        steps = []
        if text:
            steps.append({"kind": "text", "state": "pending"})
        steps.extend(
            {"kind": "image", "attachment_id": item["image_id"], "state": "pending"}
            for item in images
        )
        operation = {
            "operation_id": str(uuid.uuid4()),
            "account_id": account_id,
            "kind": "send",
            "state": "queued",
            "steps": steps,
            "payload": payload,
            "created_at": now(),
            "conversation_id": target_id if target_kind == "conversation" else None,
        }
        operation, replay = await self.store.insert_operation(
            operation, idempotency_key, fingerprint
        )
        self._wake.set()
        return _public_operation(operation, replay=replay)

    async def _run_worker(self):
        while not self._closing:
            self._wake.clear()
            try:
                if self._storage_failed:
                    # Only this worker dispatches sends. A running record here
                    # belongs to its interrupted attempt, never concurrent work.
                    for orphan in await self.store.operations({"running"}):
                        if orphan["kind"] != "send":
                            continue
                        if any(
                            s["state"] in {"dispatching", "outcome_unknown"}
                            for s in orphan["steps"]
                        ):
                            orphan["state"] = "outcome_unknown"
                            for step in orphan["steps"]:
                                if step["state"] == "dispatching":
                                    step["state"] = "outcome_unknown"
                        elif all(s["state"] == "sent" for s in orphan["steps"]):
                            orphan["state"] = "sent"
                        else:
                            orphan["state"] = "queued"
                        await self.store.save_operation(orphan)
                        await self._archive_confirmed_steps(orphan)
                for operation in await self.store.operations({"queued"}):
                    if operation["kind"] == "send":
                        await self._dispatch(operation)
                self._storage_failed = False
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not accept work into a dead worker. Revisit only durable
                # queued records; dispatching/running records require recovery.
                self._storage_failed = True
                await asyncio.sleep(1)
                self._wake.set()
            if not self._closing:
                await self._wake.wait()

    async def _check_test_policy(self, account, target, payload):
        if not self.settings.test_mode:
            return
        recipient = str(target.get("recipient_user_id") or "")
        connected = [
            a
            for a in await self.store.accounts()
            if a["account_id"] in self.settings.test_account_ids
            and a.get("user_id") in self.settings.test_user_ids
        ]
        own_users = {a.get("user_id") for a in connected}
        allowed = (
            account["account_id"] in self.settings.test_account_ids
            and account.get("user_id") in self.settings.test_user_ids
            and recipient in self.settings.test_user_ids
            and recipient in own_users
            and recipient != account.get("user_id")
            and len(own_users) >= 2
        )
        if payload["target_kind"] == "listing":
            numeric = payload["target_id"].split("-")[0]
            allowed = allowed and any(
                x.split("-")[0] == numeric for x in self.settings.test_listing_ids
            )
        if not allowed:
            raise MessagingError(
                "test_recipient_denied",
                "Testversand ist nur zwischen den festgelegten eigenen Accounts und Anzeigen erlaubt.",
                403,
            )

    async def _archive_confirmed_steps(self, operation):
        """Rebuild archive entries from durable receipts after an interrupted commit."""
        conversation_id = operation.get("conversation_id")
        steps = [
            s
            for s in operation["steps"]
            if s["state"] == "sent" and s.get("message_id")
        ]
        if not conversation_id or not steps:
            return
        account_id = operation["account_id"]
        account = await self.store.account(account_id)
        messages = []
        for step in steps:
            image = (
                await self.store.image(account_id, step["attachment_id"])
                if step["kind"] == "image"
                else None
            )
            messages.append(
                {
                    "message_id": step["message_id"],
                    "sender_user_id": account["user_id"],
                    "text": operation["payload"]["text"]
                    if step["kind"] == "text"
                    else None,
                    "created_at": step.get("sent_at"),
                    **_sent_image_references(image, step),
                }
            )
        await self.store.archive_conversations(
            account_id,
            [
                {
                    "conversation_id": conversation_id,
                    "recipient_user_id": operation.get("target", {}).get(
                        "recipient_user_id"
                    ),
                }
            ],
        )
        await self.store.archive_messages(account_id, conversation_id, messages)

    async def _dispatch(self, operation):
        account_id = operation["account_id"]
        async with self._account_locks[account_id]:
            try:
                account = await self._connected(account_id)
                payload = operation["payload"]
                operation["state"] = "running"
                await self.store.save_operation(operation)
                kind = (
                    "conversation"
                    if operation.get("conversation_id")
                    else payload["target_kind"]
                )
                target_id = operation.get("conversation_id") or payload["target_id"]
                target = await self.adapter.prepare_send(account, kind, target_id)
                self._validate_target(account, target, kind, target_id)
                await self._check_test_policy(account, target, payload)
                if (
                    operation.get("target")
                    and operation["target"]["recipient_user_id"]
                    != target["recipient_user_id"]
                ):
                    raise MessagingError(
                        "recipient_mismatch",
                        "Der Empfänger hat sich seit Annahme des Versandauftrags geändert.",
                        409,
                    )
                operation["target"] = target
                for step in operation["steps"]:
                    if step["state"] == "sent":
                        continue
                    if step["state"] != "pending":
                        raise MessagingError(
                            "outcome_unknown",
                            "Ein früherer Versandversuch ist nicht eindeutig abgeschlossen.",
                            409,
                        )
                    image = None
                    if step["kind"] == "image":
                        image = await self.store.image(
                            account_id, step["attachment_id"]
                        )
                        # Content-bound idempotency remains true even after local file modification.
                        await self.get_message_image(account_id, step["attachment_id"])
                    if step["kind"] == "image" and not target.get(
                        "capabilities", {}
                    ).get("can_send_images", False):
                        raise MessagingError(
                            "images_unsupported",
                            "Diese Nachrichtenoberfläche unterstützt hier keinen nachgewiesenen Bildversand.",
                            422,
                        )
                    if step["kind"] == "text" and not target.get(
                        "capabilities", {}
                    ).get("can_send_text", False):
                        raise MessagingError(
                            "text_unsupported",
                            "Hier ist kein Nachrichtenversand möglich.",
                            422,
                        )

                    async def before_dispatch():
                        if step["state"] not in {"pending", "dispatching"}:
                            raise MessagingError(
                                "duplicate_dispatch",
                                "Ein Versandversuch wurde bereits begonnen.",
                                409,
                            )
                        self._validate_target(account, target, kind, target_id)
                        await self._check_test_policy(account, target, payload)
                        step["state"] = "dispatching"
                        step.setdefault("dispatch_started_at", now())
                        await self.store.save_operation(operation)

                    async def on_image_prepared(value):
                        # The extension awaits this durable ACK before its sole
                        # send click. These are the actual composer preview bytes
                        # and optionally their exact, unscaled decoded raster.
                        if step["kind"] != "image" or step["state"] != "dispatching" or image is None:
                            raise MessagingError("image_unavailable", "Für diesen Schritt ist keine Bildvorschau freigegeben.", 409)
                        try:
                            preview = image_preview(value)
                        except ValueError:
                            raise MessagingError("image_unavailable", "Die Bildvorschau ist nicht eindeutig belegt.", 409) from None
                        previous = step.get("prepared_image_sha256")
                        if previous is not None and (
                            previous != preview["preview_sha256"]
                            or step.get("prepared_image_pixels") != preview.get("image_pixels")
                            or step.get("prepared_attachment_sha256") != image["sha256"]
                        ):
                            raise MessagingError("outcome_unknown", "Die bereits gespeicherte Bildvorschau hat sich verändert.", 409)
                        step["prepared_image_sha256"] = preview["preview_sha256"]
                        step["prepared_attachment_sha256"] = image["sha256"]
                        if "image_pixels" in preview:
                            step["prepared_image_pixels"] = preview["image_pixels"]
                        step.setdefault("image_prepared_at", now())
                        await self.store.save_operation(operation)

                    async def on_conversation_observed(observation):
                        # Persist navigation and image observations before the
                        # separate read. Neither observation is a send receipt.
                        is_image = step["kind"] == "image" and kind == "conversation"
                        if (
                            step["state"] != "dispatching"
                            or not (
                                (kind == "listing" and step["kind"] == "text")
                                or is_image
                            )
                            or not isinstance(observation, dict)
                        ):
                            raise MessagingError(
                                "outcome_unknown",
                                "Eine Gesprächsbeobachtung passt nicht zum laufenden Versand.",
                                409,
                            )
                        observed_id = validate_id(observation.get("conversation_id"))
                        message_id = observation.get("message_id")
                        if message_id is not None:
                            validate_id(message_id)
                        image_hash = observation.get("image_sha256")
                        pixels = None
                        if is_image and "image_pixels" in observation:
                            try:
                                pixels = pixel_proof(observation["image_pixels"])
                            except ValueError:
                                raise MessagingError("outcome_unknown", "Der Pixelbeleg der Bildbeobachtung ist ungültig.", 409) from None
                        if is_image and (
                            observation.get("kind") != "image"
                            or observed_id != target_id
                            or observed_id != target.get("conversation_id")
                            or message_id is None
                            or not isinstance(image_hash, str)
                            or re.fullmatch(r"[a-f0-9]{64}", image_hash) is None
                            or image is None
                        ):
                            raise MessagingError(
                                "outcome_unknown",
                                "Die Bildbeobachtung gehört nicht eindeutig zum Versandauftrag.",
                                409,
                            )
                        if is_image and (
                            step.get("prepared_image_sha256") is not None or pixels is not None
                        ) and (
                            image_hash != step.get("prepared_image_sha256")
                            or pixels != step.get("prepared_image_pixels")
                            or image["sha256"] != step.get("prepared_attachment_sha256")
                        ):
                            raise MessagingError("outcome_unknown", "Die Bildbeobachtung stimmt nicht mit der vor dem Klick gespeicherten Vorschau überein.", 409)
                        if (
                            operation.get("observed_conversation_id")
                            not in {None, observed_id}
                            or step.get("observed_message_id") not in {None, message_id}
                            or (
                                is_image
                                and step.get("observed_image_sha256")
                                not in {None, image_hash}
                            )
                            or (
                                is_image
                                and "observed_image_pixels" in step
                                and step["observed_image_pixels"] != pixels
                            )
                        ):
                            raise MessagingError(
                                "outcome_unknown",
                                "Es wurden widersprüchliche Gesprächsziele beobachtet.",
                                409,
                            )
                        operation["observed_conversation_id"] = observed_id
                        step["observed_conversation_id"] = observed_id
                        if message_id is not None:
                            step["observed_message_id"] = message_id
                        if is_image:
                            # The website may transcode the selected upload.
                            # Keep its immutable source and UI-preview hashes
                            # separately bound to this exact send step.
                            step["observed_image_sha256"] = image_hash
                            step["observed_attachment_sha256"] = image["sha256"]
                            if pixels is not None:
                                step["observed_image_pixels"] = pixels
                        await self.store.save_operation(operation)

                    send_options = {}
                    if (
                        getattr(
                            self.adapter, "supports_conversation_observation", False
                        )
                        is True
                    ):
                        send_options["on_conversation_observed"] = (
                            on_conversation_observed
                        )
                    if step["kind"] == "image" and getattr(self.adapter, "supports_image_preparation", False) is True:
                        send_options["on_image_prepared"] = on_image_prepared

                    receipt = await self.adapter.send_step(
                        account,
                        target,
                        step["kind"],
                        text=payload["text"] if step["kind"] == "text" else None,
                        image=image,
                        before_dispatch=before_dispatch,
                        **send_options,
                    )
                    if (
                        step["state"] != "dispatching"
                        or not receipt.get("message_id")
                        or not receipt.get("conversation_id")
                    ):
                        raise MessagingError(
                            "outcome_unknown",
                            "Der Versand konnte nicht eindeutig bestätigt werden.",
                            409,
                        )
                    if receipt.get("recipient_user_id") != target["recipient_user_id"]:
                        raise MessagingError(
                            "outcome_unknown",
                            "Die Versandbestätigung enthält keinen eindeutigen Empfänger.",
                            409,
                        )
                    if (
                        operation.get("conversation_id")
                        and receipt["conversation_id"] != operation["conversation_id"]
                    ):
                        raise MessagingError(
                            "outcome_unknown",
                            "Die Versandbestätigung gehört nicht zum erwarteten Gespräch.",
                            409,
                        )
                    if step["kind"] == "image" and send_options:
                        if (
                            receipt.get("kind") != "image"
                            or not step.get("observed_image_sha256")
                            or receipt["message_id"] != step.get("observed_message_id")
                            or receipt["conversation_id"]
                            != step.get("observed_conversation_id")
                            or image is None
                            or image["sha256"] != step.get("observed_attachment_sha256")
                        ):
                            raise MessagingError(
                                "outcome_unknown",
                                "Die Bildbestätigung passt nicht zum dauerhaft gespeicherten Versandversuch.",
                                409,
                            )
                        try:
                            evidence = receipt_evidence(
                                receipt.get("image_evidence"), step["observed_image_sha256"],
                                step.get("observed_image_pixels"),
                            )
                        except ValueError:
                            raise MessagingError("outcome_unknown", "Der empfangene Bildbeleg stimmt nicht exakt mit der gespeicherten Vorschau überein.", 409) from None
                        step["image_evidence"] = {
                            **evidence,
                            "attachment_sha256": image["sha256"],
                            "preview_sha256": step["observed_image_sha256"],
                            "message_created_at": receipt.get("created_at"),
                            "confirmed_at": now(),
                        }
                    step.update(
                        state="sent", message_id=receipt["message_id"], sent_at=now()
                    )
                    operation["conversation_id"] = receipt["conversation_id"]
                    await self.store.save_operation(operation)
                    message = {
                        "message_id": receipt["message_id"],
                        "sender_user_id": account["user_id"],
                        "text": payload["text"] if step["kind"] == "text" else None,
                        "created_at": receipt.get("created_at") or now(),
                        **_sent_image_references(image, step),
                    }
                    await self.store.archive_conversations(
                        account_id,
                        [
                            {
                                "conversation_id": receipt["conversation_id"],
                                "recipient_user_id": target["recipient_user_id"],
                            }
                        ],
                    )
                    await self.store.archive_messages(
                        account_id, receipt["conversation_id"], [message]
                    )
                    if any(s["state"] == "pending" for s in operation["steps"]):
                        kind, target_id = "conversation", receipt["conversation_id"]
                        target = await self.adapter.prepare_send(
                            account, "conversation", receipt["conversation_id"]
                        )
                        self._validate_target(account, target, kind, target_id)
                        if (
                            target["recipient_user_id"]
                            != operation["target"]["recipient_user_id"]
                        ):
                            raise MessagingError(
                                "recipient_mismatch",
                                "Der Empfänger hat sich zwischen den Versandschritten geändert.",
                                409,
                            )
                        await self._check_test_policy(account, target, payload)
                operation["state"] = "sent"
                operation.pop("error", None)
                await self.store.save_operation(operation)
            except (Exception, asyncio.CancelledError) as exc:
                dispatching = any(
                    s["state"] in {"dispatching", "outcome_unknown"}
                    for s in operation["steps"]
                )
                if dispatching:
                    operation["state"] = "outcome_unknown"
                    for step in operation["steps"]:
                        if step["state"] == "dispatching":
                            step["state"] = "outcome_unknown"
                elif operation["steps"] and all(
                    s["state"] == "sent" for s in operation["steps"]
                ):
                    operation["state"] = "sent"
                    operation["archive_warning"] = (
                        "Versand bestätigt; die lokale Archivierung konnte nicht vollständig abgeschlossen werden."
                    )
                elif isinstance(exc, MessagingError) and exc.code == "reauth_required":
                    operation["state"] = "reauth_required"
                    account = await self.store.account(account_id)
                    account["state"] = "reauth_required"
                    await self.store.save_account(account)
                elif isinstance(exc, asyncio.CancelledError):
                    operation["state"] = "queued"
                else:
                    operation["state"] = (
                        "partial"
                        if any(s["state"] == "sent" for s in operation["steps"])
                        else "failed"
                    )
                operation["error"] = self._error(exc)
                await self.store.save_operation(operation)
                if isinstance(exc, asyncio.CancelledError):
                    raise

    @staticmethod
    def _validate_target(account, target, kind, target_id):
        if (
            not target.get("recipient_user_id")
            or target.get("account_user_id") != account["user_id"]
        ):
            raise MessagingError(
                "recipient_unverified",
                "Account oder Empfänger konnten nicht eindeutig geprüft werden.",
                409,
            )
        field = "conversation_id" if kind == "conversation" else "listing_id"
        if (
            target.get(field) != target_id
            or target.get("target_kind") != kind
            or target.get("target_id") != target_id
        ):
            raise MessagingError(
                "target_mismatch",
                "Die geöffnete Anzeige oder Unterhaltung stimmt nicht mit dem Auftrag überein.",
                409,
            )

    @staticmethod
    def _error(exc):
        if isinstance(exc, _ACCESS_BLOCK_ERRORS):
            return {
                "code": exc.error_code,
                "message": "Kleinanzeigen hat den Zugriff gesperrt oder begrenzt. Keine automatischen Anmeldeversuche.",
                "retry_after_seconds": exc.retry_after_seconds,
            }
        if isinstance(exc, MessagingError):
            result = exc.to_dict()
            diagnostic = image_structure(result.pop("image_structure", None))
            if diagnostic is not None:
                result["image_structure"] = diagnostic
            return result
        return {
            "code": "messaging_interrupted"
            if isinstance(exc, asyncio.CancelledError)
            else "messaging_failed",
            "message": "Der Nachrichtenauftrag wurde unterbrochen."
            if isinstance(exc, asyncio.CancelledError)
            else "Der Nachrichtenauftrag konnte nicht abgeschlossen werden.",
        }

    async def get_operation(self, account_id, operation_id):
        operation = await self.store.operation(account_id, operation_id)
        reconciliation_id = operation.get("conversation_id") or operation.get(
            "observed_conversation_id"
        )
        # Reconciliation is read-only. Text similarity is never proof of delivery.
        if (
            operation["state"] == "outcome_unknown"
            and reconciliation_id
            and not operation.get("reconciliation")
        ):
            try:
                result = await self.get_conversation(account_id, reconciliation_id)
                recipient_matches = result.get("recipient_user_id") == operation.get(
                    "target", {}
                ).get("recipient_user_id")
                operation["reconciliation"] = {
                    "checked_at": now(),
                    "messages_seen": len(result["messages"]),
                    "automatic_resend": False,
                    "recipient_matches": recipient_matches,
                    "result": "Gespräch aktualisiert; ohne eindeutige Versandbestätigung bleibt der Ausgang unklar."
                    if recipient_matches
                    else "Das beobachtete Gespräch konnte dem vorgesehenen Empfänger nicht zugeordnet werden; der Versandausgang bleibt unklar.",
                }
                await self.store.save_operation(operation)
            except Exception:
                pass
        return _public_operation(operation)
