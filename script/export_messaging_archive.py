"""Export one account's local archive as portable HTML, JSON and image files.

Uses only the Python standard library. The source SQLite database is opened
read-only and copied with SQLite's backup API; no browser or API is contacted.
"""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import errno
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MIME_EXTENSIONS = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"
}
HTML_LABELS = {
    "listing_provider": "Anbieter", "listing_imprint": "Impressum", "profile": "Profil",
    "inbound": "Empfangen", "outbound": "Gesendet",
    "captured": "Gesichert", "not_present": "Nicht vorhanden", "unavailable": "Nicht verfügbar",
    "identity_mismatch": "Anbieterzuordnung stimmt nicht überein",
    "not_counterpart": "Gehört nicht zur Gegenpartei",
}
# Session state is deliberately outside this export. Keep the account identity
# needed to interpret messages, but never copy account profiles or operations.
ACCOUNT_FIELDS = ("account_id", "label", "user_id", "created_at")
PRIVATE_KEYS = frozenset({
    "path", "password", "passwd", "cookies", "cookie", "authorization",
    "csrf", "csrfToken", "csrf_token", "access_token", "refresh_token", "token",
    "session", "session_id", "profile_id", "tab_id", "browser_profile",
})
STYLE = """body{font:16px system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;color:#172324;background:#fff}a{color:#075ba7}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}article,section{border:1px solid #ccd5d5;border-radius:.5rem;padding:1rem;margin:1rem 0}small,.muted{color:#526363}img{max-width:100%;max-height:600px}table{border-collapse:collapse;width:100%}td,th{padding:.5rem;text-align:left;border-bottom:1px solid #ddd}.warning{color:#854600}h1,h2,h3{overflow-wrap:anywhere}"""


class ExportError(Exception):
    """A deliberately authored error suitable for the command-line boundary."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if key not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _decode(row):
    value = json.loads(row[0])
    if not isinstance(value, dict):
        raise ExportError("Das Archiv enthält einen ungültigen Datensatz.")
    return value


def _read_snapshot(root, account_id):
    path = root / "messaging.sqlite3"
    if path.is_symlink() or not path.is_file():
        raise ExportError("Die lokale Archivdatenbank fehlt oder ist ein symbolischer Link.")
    source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    snapshot = sqlite3.connect(":memory:")
    try:
        deadline = time.monotonic() + 60

        def check_deadline(*_):
            if time.monotonic() > deadline:
                raise ExportError("Die Datenbanksicherung konnte nicht innerhalb von 60 Sekunden abgeschlossen werden.")

        source.backup(snapshot, pages=128, sleep=0.05, progress=check_deadline)
        row = snapshot.execute("SELECT data FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            raise ExportError("Der ausgewählte Account ist nicht im Archiv vorhanden.")
        account = _decode(row)
        if account.get("account_id") != account_id:
            raise ExportError("Die Accountzuordnung im Archiv ist widersprüchlich.")
        conversations = []
        tables = {row[0] for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for cid, raw in snapshot.execute(
            "SELECT conversation_id,data FROM conversations WHERE account_id=? ORDER BY rowid", (account_id,)
        ):
            item = _clean(_decode((raw,)))
            item["conversation_id"] = cid
            item["messages"] = [_clean(_decode(row)) for row in snapshot.execute(
                "SELECT data FROM messages WHERE account_id=? AND conversation_id=? ORDER BY rowid", (account_id, cid)
            )]
            item["messages"].sort(key=lambda message: (message.get("created_at") or "", message.get("message_id") or ""))
            item["system_events"] = []
            if "system_events" in tables:
                item["system_events"] = [_clean(_decode(row)) for row in snapshot.execute(
                    "SELECT data FROM system_events WHERE account_id=? AND conversation_id=? ORDER BY rowid", (account_id, cid)
                )]
                item["system_events"].sort(key=lambda event: (event.get("created_at") or "", event.get("event_id") or ""))
            if {"provider_snapshots", "provider_checks"} <= tables:
                item["provider_context"] = {
                    "schema_version": 1,
                    "recipient_user_id": item.get("recipient_user_id"),
                    "listing_id": item.get("listing_id"),
                    "snapshots": [_clean(_decode(row)) for row in snapshot.execute(
                        "SELECT data FROM provider_snapshots WHERE account_id=? AND conversation_id=? ORDER BY captured_at,snapshot_id", (account_id, cid)
                    )],
                    "latest_checks": [_clean(_decode(row)) for row in snapshot.execute(
                        "SELECT data FROM provider_checks WHERE account_id=? AND conversation_id=? ORDER BY kind", (account_id, cid)
                    )],
                }
            conversations.append(item)
        images = {row[0]: _decode((row[1],)) for row in snapshot.execute(
            "SELECT image_id,data FROM images WHERE account_id=? ORDER BY image_id", (account_id,)
        )}
        return {key: account[key] for key in ACCOUNT_FIELDS if key in account}, conversations, images
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
        raise ExportError("Die lokale Archivdatenbank konnte nicht vollständig gelesen werden.") from None
    finally:
        snapshot.close()
        source.close()


def _private_directory(path):
    if path.is_symlink():
        raise ExportError("Das Exportverzeichnis darf kein symbolischer Link sein.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _write(path, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _signature(data, mime):
    return {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
    }.get(mime, False)


def _copy_image(root, account_id, image_id, item, destination):
    result = _clean(item)
    result["image_id"] = image_id
    digest, mime = item.get("sha256"), item.get("mime_type")
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest) or image_id != "img_" + digest or mime not in MIME_EXTENSIONS:
        return {**result, "export_status": "invalid_metadata"}
    filename = digest + "." + MIME_EXTENSIONS[mime]
    # Derive the location from validated content identity, not stored absolute
    # paths. This also works after the entire archive has been moved to a new Mac.
    directory = root / "images" / account_id
    path = directory / filename
    if any(part.is_symlink() for part in (root / "images", directory, path)):
        return {**result, "export_status": "unsafe_path"}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_IMAGE_BYTES:
                return {**result, "export_status": "invalid_file"}
            data = source.read(MAX_IMAGE_BYTES + 1)
    except FileNotFoundError:
        return {**result, "export_status": "missing"}
    except OSError:
        return {**result, "export_status": "unreadable"}
    if len(data) != item.get("size") or hashlib.sha256(data).hexdigest() != digest or not _signature(data, mime):
        return {**result, "export_status": "integrity_failed"}
    relative = "images/" + filename
    _write(destination / relative, data)
    return {**result, "export_status": "copied", "export_file": relative}


def _escape(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _label(value):
    return _escape(HTML_LABELS.get(value, value) if isinstance(value, str) else value)


def _source(value):
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
        safe = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
    except ValueError:
        safe = False
    text = _escape(value)
    if not safe:
        return text
    return f'<a href="{text}" rel="noreferrer noopener">{text}</a>'


def _document(title, body):
    return f'''<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{_escape(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>\n'''


def _media_html(items, label):
    if not items:
        return ""
    parts = [f"<h3>{_escape(label)}</h3>"]
    for item in items:
        if item.get("export_status") == "copied":
            path = "../" + item["export_file"]
            parts.append(f'<figure><a href="{_escape(path)}"><img src="{_escape(path)}" alt="Archiviertes Bild" loading="lazy"></a><figcaption>{_escape(item.get("image_id"))}</figcaption></figure>')
        else:
            reason = item.get("export_status") or "nicht archiviert"
            parts.append(f'<p class="warning">Bild nicht verfügbar: {_escape(reason)}</p>')
    return "".join(parts)


def _conversation_html(conversation):
    title = conversation.get("title") or conversation["conversation_id"]
    parts = ['<p><a href="../index.html">Zur Übersicht</a></p>', f"<h1>{_escape(title)}</h1>"]
    parts.append(f'<p>Gespräch: {_escape(conversation["conversation_id"])}<br>Gegenpartei: {_escape(conversation.get("recipient_user_id"))}<br>Anzeige: {_escape(conversation.get("listing_id"))}</p>')
    context = conversation.get("provider_context") or {}
    parts.append("<section><h2>Anbieter- und Profilinformationen</h2>")
    if not context.get("snapshots"):
        parts.append("<p>Für dieses Gespräch sind keine Anbieter-Snapshots archiviert.</p>")
    for snapshot in context.get("snapshots", []):
        parts.append(f'<article><h3>{_label(snapshot.get("kind"))}: {_escape(snapshot.get("title"))}</h3><p>Erfasst: {_escape(snapshot.get("captured_at"))}<br>Quelle: {_source(snapshot.get("source_url"))}</p><pre>{_escape(snapshot.get("text"))}</pre>')
        if snapshot.get("truncated"):
            parts.append('<p class="warning">Diese Momentaufnahme ist als unvollständig gekennzeichnet.</p>')
        parts.append("</article>")
    if context.get("latest_checks"):
        parts.append("<h3>Letzter Abrufstatus</h3><ul>")
        for check in context["latest_checks"]:
            parts.append(f'<li>{_label(check.get("kind"))}: {_label(check.get("status"))} · {_escape(check.get("captured_at"))} · {_source(check.get("source_url"))}</li>')
        parts.append("</ul>")
    parts.append("</section><h2>Nachrichten</h2>")
    if not conversation["messages"]:
        parts.append('<p class="warning">Bisher ist nur die Gesprächsübersicht archiviert; es liegen keine gespeicherten Nachrichten vor.</p>')
    for message in conversation["messages"]:
        parts.append(f'<article><p><strong>{_escape(message.get("sender_user_id"))}</strong> · {_escape(message.get("created_at"))} · {_label(message.get("direction"))}</p><pre>{_escape(message.get("text"))}</pre>')
        if message.get("remote_content_unavailable"):
            parts.append('<p class="warning">Der frühere Text blieb im Archiv erhalten; er war beim späteren Abruf nicht mehr verfügbar.</p>')
        for previous in message.get("previous_texts", []):
            parts.append(f"<details><summary>Früher archivierter Text</summary><pre>{_escape(previous)}</pre></details>")
        parts.append(_media_html(message.get("images", []), "Plattformbilder"))
        parts.append(_media_html(message.get("upload_originals", []), "Lokale Uploadoriginale"))
        parts.append(f'<small>Nachrichten-ID: {_escape(message.get("message_id"))} · Archiviert: {_escape(message.get("retrieved_at"))}</small></article>')
    if conversation.get("system_events"):
        parts.append("<section><h2>Plattform-Systemereignisse</h2><p>Diese Ereignisse wurden von der Plattform erzeugt; sie sind keine Nachrichten einer Gesprächspartei.</p>")
        for event in conversation["system_events"]:
            parts.append(f'<article><h3>{_escape(event.get("event_type"))}</h3><p>{_escape(event.get("created_at"))}</p><pre>{_escape(event.get("text"))}</pre>')
            for previous in event.get("previous_texts", []):
                parts.append(f"<details><summary>Früher archivierter Text</summary><pre>{_escape(previous)}</pre></details>")
            parts.append(f'<small>Ereignis-ID: {_escape(event.get("event_id"))} · Archiviert: {_escape(event.get("retrieved_at"))}</small></article>')
        parts.append("</section>")
    return _document(title, "".join(parts))


def _export_name(account_id):
    return account_id + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex


def _publish(staging, destination):
    """Atomically publish a directory without replacing even an empty target."""
    if os.name == "nt":
        os.rename(staging, destination)  # Windows rename never replaces a target.
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        status = rename(os.fsencode(staging), os.fsencode(destination), 0x4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        status = rename(-100, os.fsencode(staging), -100, os.fsencode(destination), 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise ExportError("Dieses System unterstützt keine sichere atomare Veröffentlichung des Exportordners.")
    if status:
        code = ctypes.get_errno()
        if code in {errno.EEXIST, errno.ENOTEMPTY}:
            raise ExportError("Der vorgesehene Exportpfad existiert bereits.")
        raise OSError(code, "Der vollständige Exportordner konnte nicht veröffentlicht werden.")


def export_archive(root, account_id, output_parent=None):
    """Return a newly created portable export directory; never overwrite exports."""
    if not isinstance(account_id, str) or not ACCOUNT.fullmatch(account_id):
        raise ExportError("Ungültige Accountkennung.")
    root = Path(root).expanduser().absolute()
    if root.is_symlink():
        raise ExportError("Der Archivstamm darf kein symbolischer Link sein.")
    root = root.resolve()
    account, conversations, image_rows = _read_snapshot(root, account_id)
    parent = Path(output_parent).expanduser().absolute() if output_parent is not None else root / "exports"
    _private_directory(parent)
    destination = parent / _export_name(account_id)
    if destination.exists() or destination.is_symlink():
        raise ExportError("Der vorgesehene Exportpfad existiert bereits.")
    staging = Path(tempfile.mkdtemp(prefix=".export-", dir=parent))
    try:
        for name in ("images", "conversations"):
            _private_directory(staging / name)
        library = {key: _copy_image(root, account_id, key, value, staging) for key, value in image_rows.items()}
        unresolved = []
        for conversation in conversations:
            for message in conversation["messages"]:
                for field in ("images", "upload_originals"):
                    for item in message.get(field, []):
                        stored = library.get(item.get("image_id"))
                        if stored is None:
                            item["export_status"] = "missing_metadata" if item.get("image_id") else "not_archived"
                        else:
                            item.update({key: stored[key] for key in ("export_status", "export_file") if key in stored})
                        if item["export_status"] != "copied":
                            unresolved.append({"conversation_id": conversation["conversation_id"], "message_id": message.get("message_id"), "kind": field, "image_id": item.get("image_id"), "status": item["export_status"]})
            conversation["export_file"] = "conversations/" + hashlib.sha256(conversation["conversation_id"].encode()).hexdigest() + ".html"
            _write(staging / conversation["export_file"], _conversation_html(conversation))
        exported_at = _now()
        archive = {
            "schema_version": 1, "exported_at": exported_at, "source": "local_archive",
            "scope": "actually_archived_content", "account": account,
            "conversations": conversations, "image_library": list(library.values()),
        }
        _write(staging / "archive.json", _json(archive))
        rows = []
        for item in conversations:
            rows.append(f'<tr><td><a href="{_escape(item["export_file"])}">{_escape(item.get("title") or item["conversation_id"])}</a></td><td>{len(item["messages"])}</td><td>{_escape(item.get("updated_at") or item.get("retrieved_at"))}</td></tr>')
        body = f'<h1>Kleinanzeigen-Nachrichtenarchiv</h1><p>Account: {_escape(account.get("label") or account_id)}<br>Exportiert: {_escape(exported_at)}</p><p>Dieser Export enthält ausschließlich tatsächlich lokal gespeicherte Inhalte. Er ist ohne Kleinanzeigen-Anmeldung und ohne laufenden API-Server lesbar. Quellenlinks können später nicht mehr erreichbar sein.</p><p><a href="archive.json">Strukturierte Daten (JSON)</a> · <a href="manifest.json">Dateiverzeichnis und Prüfsummen</a> · <a href="README.txt">Hinweise zur Aufbewahrung</a></p><p>{len(conversations)} Gespräche · {sum(len(item["messages"]) for item in conversations)} Nachrichten · {len(unresolved)} nicht verfügbare Bildreferenzen</p><table><thead><tr><th>Gespräch</th><th>Nachrichten</th><th>Letzter Stand</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
        _write(staging / "index.html", _document("Kleinanzeigen-Nachrichtenarchiv", body))
        _write(staging / "README.txt", "Kleinanzeigen-Nachrichtenarchiv\n\nÖffne index.html in einem Browser. Eine Anmeldung, Internetverbindung oder laufende API ist dafür nicht nötig. archive.json enthält die strukturierten Gesprächsdaten, Anbieter-Snapshots und Bildreferenzen; images/ enthält die kopierten Bilddateien. manifest.json verzeichnet Dateigrößen, SHA-256-Prüfsummen und fehlende Bilder.\n\nDer Export umfasst ausschließlich tatsächlich lokal archivierte Inhalte. Bereits vor dem Abruf gelöschte oder nicht erreichbare Inhalte können fehlen. Fehlgeschlagene Bildabrufe und Anbieterprüfungen sind ausdrücklich ausgewiesen. Frühere gespeicherte Inhalte bleiben enthalten.\n\nDieser Ordner ist eine transportable Kopie auf demselben Datenträger, noch keine unabhängige Datensicherung. Kopiere den vollständigen Ordner auf ein weiteres gesichertes Speichermedium beziehungsweise in dein vorhandenes Backup. Bewahre alle Unterordner zusammen auf. Keine Anmeldedaten, Browserprofile, Cookies oder Versandaufträge sind Bestandteil dieses Exports. Die Quelldatenbank wird nicht verändert.\n")
        files = {}
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                data = path.read_bytes()
                files[path.relative_to(staging).as_posix()] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        manifest = {
            "schema_version": 1, "exported_at": exported_at, "account_id": account_id,
            "scope": "actually_archived_content", "files": files,
            "counts": {"conversations": len(conversations), "messages": sum(len(item["messages"]) for item in conversations), "system_events": sum(len(item["system_events"]) for item in conversations), "copied_images": sum(item["export_status"] == "copied" for item in library.values()), "unavailable_image_references": len(unresolved)},
            "unavailable_image_references": unresolved,
            "image_library_failures": [{"image_id": key, "status": value["export_status"]} for key, value in library.items() if value["export_status"] != "copied"],
        }
        _write(staging / "manifest.json", _json(manifest))
        for directory in (staging / "conversations", staging / "images", staging):
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        _publish(staging, destination)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return destination
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main():
    parser = argparse.ArgumentParser(description="Ein lokales Accountarchiv als HTML, JSON und Bilder exportieren; kein Browserzugriff.")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--archive-root", type=Path, default=ROOT / "data/messaging")
    parser.add_argument("--output-parent", type=Path, default=None)
    args = parser.parse_args()
    try:
        path = export_archive(args.archive_root, args.account_id, args.output_parent)
    except (ExportError, OSError) as error:
        parser.exit(1, f"Export fehlgeschlagen: {error}\n")
    print(path)


if __name__ == "__main__":
    main()
