"""Private SQLite archive and dispatch journal, independent of job retention."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone
import warnings

from PIL import Image, UnidentifiedImageError

from .contracts import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, MessagingError, validate_id
from .provider_context import validate_provider_context
from .system_events import validate_system_events


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def private_directory(path: Path):
    if path.is_symlink():
        raise MessagingError(
            "unsafe_storage", "Der Speicherpfad darf kein symbolischer Link sein.", 503
        )
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


class MessagingStore:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        self.db = None
        self._file_lock = None
        self.lock = asyncio.Lock()

    async def start(self):
        private_directory(self.root)
        private_directory(self.root / "images")
        private_directory(self.root / "profiles")
        lock_path = self.root / "messaging.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self._file_lock = os.fdopen(fd, "a+b")
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file_lock.close()
            self._file_lock = None
            raise MessagingError(
                "storage_in_use", "Der Nachrichtenspeicher wird bereits verwendet.", 503
            ) from None
        db_path = self.root / "messaging.sqlite3"
        if db_path.is_symlink():
            await self.close()
            raise MessagingError("unsafe_storage", "Ungültiger Datenbankpfad.", 503)
        fd = os.open(db_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        db_path.chmod(0o600)
        self.db = sqlite3.connect(db_path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversations (
                account_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                data TEXT NOT NULL, PRIMARY KEY(account_id, conversation_id));
            CREATE TABLE IF NOT EXISTS messages (
                account_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                message_id TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(account_id, conversation_id, message_id));
            CREATE TABLE IF NOT EXISTS images (
                account_id TEXT NOT NULL, image_id TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(account_id, image_id));
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                idempotency_key TEXT, fingerprint TEXT, state TEXT NOT NULL,
                data TEXT NOT NULL, UNIQUE(account_id, idempotency_key));
            CREATE INDEX IF NOT EXISTS operations_state ON operations(state);
            CREATE TABLE IF NOT EXISTS provider_snapshots (
                account_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL, captured_at TEXT NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY(account_id, conversation_id, snapshot_id));
            CREATE TABLE IF NOT EXISTS provider_checks (
                account_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                kind TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(account_id, conversation_id, kind));
            CREATE TABLE IF NOT EXISTS system_events (
                account_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                event_id TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(account_id, conversation_id, event_id));
            PRAGMA user_version=2;
        """)

    async def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self._file_lock is not None:
            self._file_lock.close()
            self._file_lock = None

    async def account(self, account_id):
        validate_id(account_id, account=True)
        async with self.lock:
            row = self.db.execute(
                "SELECT data FROM accounts WHERE account_id=?", (account_id,)
            ).fetchone()
        if not row:
            raise MessagingError("account_not_found", "Account nicht gefunden.", 404)
        return json.loads(row[0])

    async def save_account(self, account):
        validate_id(account["account_id"], account=True)
        account = {**account, "updated_at": now()}
        async with self.lock:
            self.db.execute(
                "INSERT INTO accounts VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET data=excluded.data",
                (account["account_id"], encode(account)),
            )
        return account

    async def accounts(self):
        async with self.lock:
            rows = self.db.execute(
                "SELECT data FROM accounts ORDER BY account_id"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    async def insert_operation(self, operation, key=None, fingerprint=None):
        async with self.lock:
            if key is not None:
                row = self.db.execute(
                    "SELECT fingerprint,data FROM operations WHERE account_id=? AND idempotency_key=?",
                    (operation["account_id"], key),
                ).fetchone()
                if row:
                    if row[0] != fingerprint:
                        raise MessagingError(
                            "idempotency_conflict",
                            "Dieser Schlüssel gehört zu einem anderen Nachrichtenauftrag.",
                            409,
                        )
                    return json.loads(row[1]), True
            self.db.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?)",
                (
                    operation["operation_id"],
                    operation["account_id"],
                    key,
                    fingerprint,
                    operation["state"],
                    encode(operation),
                ),
            )
        return operation, False

    async def save_operation(self, operation):
        operation = {**operation, "updated_at": now()}
        async with self.lock:
            self.db.execute(
                "UPDATE operations SET state=?,data=? WHERE operation_id=? AND account_id=?",
                (
                    operation["state"],
                    encode(operation),
                    operation["operation_id"],
                    operation["account_id"],
                ),
            )
        return operation

    async def operation(self, account_id, operation_id):
        validate_id(account_id, account=True)
        validate_id(operation_id)
        async with self.lock:
            row = self.db.execute(
                "SELECT data FROM operations WHERE account_id=? AND operation_id=?",
                (account_id, operation_id),
            ).fetchone()
        if row is None:
            raise MessagingError(
                "operation_not_found", "Nachrichtenauftrag nicht gefunden.", 404
            )
        return json.loads(row[0])

    async def operations(self, states):
        async with self.lock:
            rows = self.db.execute(
                "SELECT data FROM operations WHERE state IN ("
                + ",".join("?" for _ in states)
                + ") ORDER BY rowid",
                tuple(states),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    async def image(self, account_id, image_id):
        validate_id(account_id, account=True)
        validate_id(image_id)
        async with self.lock:
            row = self.db.execute(
                "SELECT data FROM images WHERE account_id=? AND image_id=?",
                (account_id, image_id),
            ).fetchone()
        if not row:
            raise MessagingError(
                "image_not_found", "Bild für diesen Account nicht gefunden.", 404
            )
        return json.loads(row[0])

    async def save_image(self, account_id, data, filename="image"):
        validate_id(account_id, account=True)
        await self.account(account_id)
        if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
            raise MessagingError(
                "invalid_image_size",
                "Bilder müssen zwischen 1 Byte und 20 MiB groß sein.",
                413,
            )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as img:
                    fmt = img.format
                    width, height = img.size
                    if width * height > MAX_IMAGE_PIXELS:
                        raise ValueError("pixel limit")
                    img.verify()
                # verify() checks container structure; load() also detects
                # truncated pixel data (notably JPEGs) within the pixel limit.
                with Image.open(io.BytesIO(data)) as img:
                    img.load()
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            SyntaxError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            raise MessagingError(
                "invalid_image", "Ungültiges oder zu großes Bild.", 422
            ) from None
        formats = {
            "PNG": ("image/png", "png"),
            "JPEG": ("image/jpeg", "jpg"),
            "GIF": ("image/gif", "gif"),
            "WEBP": ("image/webp", "webp"),
        }
        if fmt not in formats:
            raise MessagingError(
                "unsupported_image", "Unterstützt werden PNG, JPEG, GIF und WebP.", 422
            )
        mime, extension = formats[fmt]
        digest = hashlib.sha256(data).hexdigest()
        image_id = "img_" + digest
        directory = self.root / "images" / account_id
        private_directory(directory)
        path = directory / f"{digest}.{extension}"
        if path.is_symlink():
            raise MessagingError("unsafe_storage", "Ungültiger Bildpfad.", 503)
        if not path.exists():
            fd, temp = tempfile.mkstemp(dir=directory, prefix=".upload-")
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp, path)
                dirfd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        path.chmod(0o600)
        item = {
            "image_id": image_id,
            "attachment_id": image_id,
            "sha256": digest,
            "mime_type": mime,
            "size": len(data),
            "width": width,
            "height": height,
            "path": str(path),
            "created_at": now(),
        }
        async with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO images VALUES(?,?,?)",
                (account_id, image_id, encode(item)),
            )
        return await self.image(account_id, image_id)

    async def archive_conversations(self, account_id, conversations):
        async with self.lock:
            for item in conversations:
                # Provider snapshots live separately; summaries must not replace
                # captured history with a partial or unavailable page response.
                item = {
                    key: value for key, value in item.items()
                    if key not in {"provider_context", "provider_context_error", "system_events", "system_events_error"}
                }
                cid = validate_id(item["conversation_id"])
                old = self.db.execute(
                    "SELECT data FROM conversations WHERE account_id=? AND conversation_id=?",
                    (account_id, cid),
                ).fetchone()
                previous = json.loads(old[0]) if old else {}
                for key in ("recipient_user_id", "listing_id"):
                    if previous.get(key) and item.get(key) and previous[key] != item[key]:
                        raise MessagingError(
                            "conversation_mismatch",
                            "Die Gesprächszuordnung stimmt nicht mit dem Archiv überein.",
                            409,
                        )
                    if previous.get(key) and not item.get(key):
                        item[key] = previous[key]
                data = {
                    **previous,
                    **item,
                    "retrieved_at": now(),
                }
                self.db.execute(
                    "INSERT INTO conversations VALUES(?,?,?) ON CONFLICT(account_id,conversation_id) DO UPDATE SET data=excluded.data",
                    (account_id, cid, encode(data)),
                )

    async def archive_provider_context(self, account_id, conversation_id, context):
        """Append distinct observations; failures only update their check status."""
        validate_id(account_id, account=True)
        validate_id(conversation_id)
        async with self.lock:
            row = self.db.execute(
                "SELECT data FROM conversations WHERE account_id=? AND conversation_id=?",
                (account_id, conversation_id),
            ).fetchone()
            if row is None:
                raise MessagingError("conversation_not_found", "Gespräch nicht im Archiv gefunden.", 404)
            meta = json.loads(row[0])
            sections = validate_provider_context(
                context, meta.get("recipient_user_id"), meta.get("listing_id")
            )
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for section in sections:
                    check = dict(section)
                    if section["status"] == "captured":
                        payload = {
                            key: value for key, value in section.items()
                            if key not in {"status", "captured_at"}
                        }
                        snapshot_id = "provider_" + hashlib.sha256(encode(payload).encode()).hexdigest()
                        snapshot = {
                            **payload, "snapshot_id": snapshot_id,
                            "captured_at": section["captured_at"],
                        }
                        self.db.execute(
                            "INSERT OR IGNORE INTO provider_snapshots VALUES(?,?,?,?,?)",
                            (account_id, conversation_id, snapshot_id,
                             section["captured_at"], encode(snapshot)),
                        )
                        check = {
                            key: value for key, value in section.items()
                            if key not in {"text", "title", "truncated"}
                        }
                        check["snapshot_id"] = snapshot_id
                    self.db.execute(
                        "INSERT INTO provider_checks VALUES(?,?,?,?) "
                        "ON CONFLICT(account_id,conversation_id,kind) DO UPDATE SET data=excluded.data",
                        (account_id, conversation_id, section["kind"], encode(check)),
                    )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    async def archived_provider_context(self, account_id, conversation_id):
        validate_id(account_id, account=True)
        validate_id(conversation_id)
        async with self.lock:
            rows = self.db.execute(
                "SELECT data FROM provider_snapshots WHERE account_id=? AND conversation_id=? "
                "ORDER BY captured_at,rowid", (account_id, conversation_id),
            ).fetchall()
            checks = self.db.execute(
                "SELECT data FROM provider_checks WHERE account_id=? AND conversation_id=? "
                "ORDER BY kind", (account_id, conversation_id),
            ).fetchall()
        if not rows and not checks:
            return None
        snapshots = [json.loads(row[0]) for row in rows]
        latest_checks = [json.loads(row[0]) for row in checks]
        binding = (snapshots or latest_checks)[0]
        return {
            "schema_version": 1,
            "recipient_user_id": binding["recipient_user_id"],
            "listing_id": binding.get("listing_id"),
            "snapshots": snapshots,
            "latest_checks": latest_checks,
        }

    async def archive_system_events(self, account_id, conversation_id, events):
        validate_id(account_id, account=True)
        validate_id(conversation_id)
        events = validate_system_events(events)
        async with self.lock:
            if self.db.execute(
                "SELECT 1 FROM conversations WHERE account_id=? AND conversation_id=?",
                (account_id, conversation_id),
            ).fetchone() is None:
                raise MessagingError("conversation_not_found", "Gespräch nicht im Archiv gefunden.", 404)
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for event in events:
                    row = self.db.execute(
                        "SELECT data FROM system_events WHERE account_id=? AND conversation_id=? AND event_id=?",
                        (account_id, conversation_id, event["event_id"]),
                    ).fetchone()
                    stored = {**event, "retrieved_at": now()}
                    if row:
                        previous = json.loads(row[0])
                        if previous["event_type"] != event["event_type"]:
                            raise MessagingError("system_events_invalid", "Die Ereigniszuordnung stimmt nicht mit dem Archiv überein.", 409)
                        stored = {**previous, **stored}
                        if not event.get("text") and previous.get("text"):
                            stored["text"] = previous["text"]
                            stored["remote_content_unavailable"] = True
                        elif event.get("text") != previous.get("text") and previous.get("text"):
                            stored["previous_texts"] = list(dict.fromkeys([
                                *previous.get("previous_texts", []), previous["text"],
                            ]))
                    self.db.execute(
                        "INSERT INTO system_events VALUES(?,?,?,?) "
                        "ON CONFLICT(account_id,conversation_id,event_id) DO UPDATE SET data=excluded.data",
                        (account_id, conversation_id, event["event_id"], encode(stored)),
                    )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return events

    async def archived_system_events(self, account_id, conversation_id):
        validate_id(account_id, account=True)
        validate_id(conversation_id)
        async with self.lock:
            rows = self.db.execute(
                "SELECT data FROM system_events WHERE account_id=? AND conversation_id=? ORDER BY rowid",
                (account_id, conversation_id),
            ).fetchall()
        events = [json.loads(row[0]) for row in rows]
        events.sort(key=lambda event: (event.get("created_at") or "", event["event_id"]))
        return events

    async def archive_messages(self, account_id, conversation_id, messages):
        async with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for item in messages:
                    mid = validate_id(item["message_id"])
                    previous = self.db.execute(
                        "SELECT data FROM messages WHERE account_id=? AND conversation_id=? AND message_id=?",
                        (account_id, conversation_id, mid),
                    ).fetchone()
                    if previous:
                        old = json.loads(previous[0])
                        merged = {**old, **item}
                        if not item.get("text") and old.get("text"):
                            merged["text"] = old["text"]
                            merged["remote_content_unavailable"] = True
                        elif item.get("text") != old.get("text") and old.get("text"):
                            merged["previous_texts"] = list(
                                dict.fromkeys(
                                    [*old.get("previous_texts", []), old["text"]]
                                )
                            )
                        known = {
                            # A content hash identifies stored bytes, not a
                            # platform attachment slot. Identical pictures may
                            # legitimately occur at different remote indices.
                            (image.get("remote_id"), image["image_id"]): image
                            for image in [
                                *old.get("images", []),
                                *item.get("images", []),
                            ]
                            if image.get("image_id")
                        }
                        known_remote_ids = {
                            image.get("remote_id")
                            for image in known.values()
                            if image.get("remote_id")
                        }
                        unavailable = [
                            image
                            for image in item.get("images", [])
                            if not image.get("image_id")
                            and image.get("remote_id") not in known_remote_ids
                        ]
                        merged["images"] = [*known.values(), *unavailable]
                        # Upload originals are separately labelled local source
                        # files. A later platform read must neither count them as
                        # another received attachment nor erase their references.
                        if "upload_originals" in old or "upload_originals" in item:
                            originals = {
                                image["image_id"]: image
                                for image in [
                                    *old.get("upload_originals", []),
                                    *item.get("upload_originals", []),
                                ]
                                if image.get("image_id")
                                and image.get("source") == "upload_original"
                            }
                            merged["upload_originals"] = list(originals.values())
                        item = merged
                    self.db.execute(
                        "INSERT INTO messages VALUES(?,?,?,?) ON CONFLICT(account_id,conversation_id,message_id) DO UPDATE SET data=excluded.data",
                        (
                            account_id,
                            conversation_id,
                            mid,
                            encode({**item, "retrieved_at": now()}),
                        ),
                    )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    async def archived_conversations(self, account_id):
        async with self.lock:
            rows = self.db.execute(
                "SELECT data FROM conversations WHERE account_id=? ORDER BY rowid DESC",
                (account_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    async def archived_conversation(self, account_id, conversation_id):
        async with self.lock:
            row = self.db.execute(
                "SELECT data FROM conversations WHERE account_id=? AND conversation_id=?",
                (account_id, conversation_id),
            ).fetchone()
            rows = self.db.execute(
                "SELECT data FROM messages WHERE account_id=? AND conversation_id=? ORDER BY rowid",
                (account_id, conversation_id),
            ).fetchall()
        if not row:
            raise MessagingError(
                "conversation_not_found", "Gespräch nicht im Archiv gefunden.", 404
            )
        conversation = json.loads(row[0])
        provider_context = await self.archived_provider_context(account_id, conversation_id)
        if provider_context is not None:
            conversation["provider_context"] = provider_context
        events = await self.archived_system_events(account_id, conversation_id)
        if events:
            conversation["system_events"] = events
        messages = [json.loads(r[0]) for r in rows]
        # ISO timestamps sort chronologically; missing times retain insertion order.
        messages.sort(
            key=lambda message: (message.get("created_at") or "", message["message_id"])
        )
        return conversation, messages
