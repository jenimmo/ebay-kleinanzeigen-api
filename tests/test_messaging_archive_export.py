"""Portable exports remain useful independently of the app and its database."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from script import export_messaging_archive as exporter


PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082")


def put(db, table, keys, value):
    db.execute("INSERT INTO " + table + " VALUES(" + ",".join("?" for _ in range(len(keys) + 1)) + ")", (*keys, json.dumps(value, ensure_ascii=False)))


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    db = sqlite3.connect(root / "messaging.sqlite3", isolation_level=None)
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE accounts(account_id TEXT PRIMARY KEY,data TEXT);
        CREATE TABLE conversations(account_id TEXT,conversation_id TEXT,data TEXT);
        CREATE TABLE messages(account_id TEXT,conversation_id TEXT,message_id TEXT,data TEXT);
        CREATE TABLE images(account_id TEXT,image_id TEXT,data TEXT);
        CREATE TABLE operations(operation_id TEXT,data TEXT);
    """)
    put(db, "accounts", ("private",), {"account_id": "private", "label": "Privat – Grüße", "user_id": "123", "cookies": "SECRET-COOKIE", "password": "SECRET-PASSWORD", "profile_id": "SECRET-PROFILE"})
    put(db, "accounts", ("business",), {"account_id": "business", "label": "OTHER-ACCOUNT"})
    put(db, "operations", ("op1",), {"text": "UNSENT-DRAFT-SECRET"})
    put(db, "conversations", ("private", "chat:1"), {"conversation_id": "chat:1", "title": '<script>alert("title")</script>', "recipient_user_id": "456", "listing_id": "12345"})
    put(db, "conversations", ("business", "chat:2"), {"conversation_id": "chat:2", "title": "OTHER-CHAT"})
    put(db, "messages", ("business", "chat:2", "m2"), {"message_id": "m2", "text": "OTHER-MESSAGE"})
    digest = hashlib.sha256(PNG).hexdigest()
    image_id = "img_" + digest
    directory = root / "images/private"
    directory.mkdir(parents=True)
    (directory / (digest + ".png")).write_bytes(PNG)
    put(db, "images", ("private", image_id), {"image_id": image_id, "sha256": digest, "mime_type": "image/png", "size": len(PNG), "path": "/obsolete/mac/images/source.png"})
    put(db, "messages", ("private", "chat:1", "m1"), {"message_id": "m1", "text": '<img src=x onerror="alert(1)"> Grüße äöü', "created_at": "2026-11-04T10:00:00Z", "images": [{"image_id": image_id}], "upload_originals": [{"image_id": image_id, "source": "upload_original"}]})
    try:
        yield root, db, image_id
    finally:
        db.close()


def test_export_is_portable_private_and_account_scoped(archive):
    root, db, image_id = archive
    before = db.execute("SELECT count(*) FROM messages").fetchone()
    destination = exporter.export_archive(root, "private")
    data = json.loads((destination / "archive.json").read_text())
    manifest = json.loads((destination / "manifest.json").read_text())
    assert data["scope"] == "actually_archived_content"
    assert len(data["conversations"]) == 1
    assert manifest["counts"] == {"conversations": 1, "messages": 1, "system_events": 0, "copied_images": 1, "unavailable_image_references": 0}
    assert data["account"] == {"account_id": "private", "label": "Privat – Grüße", "user_id": "123"}
    message = data["conversations"][0]["messages"][0]
    assert message["upload_originals"][0]["source"] == "upload_original"
    image_file = destination / message["images"][0]["export_file"]
    assert image_file.read_bytes() == PNG
    assert image_file.name == image_id[4:] + ".png"
    text_files = "\n".join(path.read_text() for path in destination.rglob("*") if path.suffix in {".json", ".html", ".txt"})
    for forbidden in ("SECRET-COOKIE", "SECRET-PASSWORD", "SECRET-PROFILE", "OTHER-ACCOUNT", "OTHER-CHAT", "OTHER-MESSAGE", "UNSENT-DRAFT-SECRET", "/obsolete/mac"):
        assert forbidden not in text_files
    page = (destination / data["conversations"][0]["export_file"]).read_text()
    assert "<script>" not in page
    assert '<img src=x onerror="alert(1)">' not in page
    assert "&lt;script&gt;" in page
    assert "Grüße äöü" in page
    assert "default-src 'none'" in page
    assert db.execute("SELECT count(*) FROM messages").fetchone() == before
    for path in [destination, *destination.rglob("*")]:
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
    for filename, facts in manifest["files"].items():
        contents = (destination / filename).read_bytes()
        assert facts == {"size": len(contents), "sha256": hashlib.sha256(contents).hexdigest()}


def test_sqlite_backup_reads_committed_wal_without_touching_pending_writer(archive):
    root, db, _ = archive
    db.execute("BEGIN IMMEDIATE")
    put(db, "messages", ("private", "chat:1", "not-committed"), {"message_id": "not-committed", "text": "UNCOMMITTED"})
    destination = exporter.export_archive(root, "private")
    assert "UNCOMMITTED" not in (destination / "archive.json").read_text()
    assert db.in_transaction
    db.execute("COMMIT")
    second = exporter.export_archive(root, "private")
    assert second != destination
    assert "UNCOMMITTED" in (second / "archive.json").read_text()


@pytest.mark.parametrize("mode,status", [("missing", "missing"), ("corrupt", "integrity_failed"), ("symlink", "unsafe_path")])
def test_media_failure_is_recorded_without_following_external_files(archive, tmp_path, mode, status):
    root, _, image_id = archive
    path = root / "images/private" / (image_id[4:] + ".png")
    path.unlink()
    if mode == "corrupt":
        path.write_bytes(PNG + b"damaged")
    elif mode == "symlink":
        external = tmp_path / "outside.txt"
        external.write_text("DO-NOT-EXPORT")
        path.symlink_to(external)
    destination = exporter.export_archive(root, "private")
    data = json.loads((destination / "archive.json").read_text())
    manifest = json.loads((destination / "manifest.json").read_text())
    assert data["image_library"][0]["export_status"] == status
    assert manifest["counts"]["unavailable_image_references"] == 2
    assert list((destination / "images").iterdir()) == []
    assert "DO-NOT-EXPORT" not in (destination / "archive.json").read_text()
    assert (destination / data["conversations"][0]["export_file"]).read_text().count("Bild nicht verfügbar") == 2


def test_unarchived_and_orphan_image_references_stay_explicit(archive):
    root, db, _ = archive
    put(db, "messages", ("private", "chat:1", "missing"), {"message_id": "missing", "images": [{"unavailable": True, "remote_id": "gone", "error": {"code": "image_unavailable"}}, {"image_id": "img_unknown"}]})
    destination = exporter.export_archive(root, "private")
    manifest = json.loads((destination / "manifest.json").read_text())
    assert {item["status"] for item in manifest["unavailable_image_references"]} == {"not_archived", "missing_metadata"}


def test_provider_history_and_latest_unavailable_check_are_preserved(archive):
    root, db, _ = archive
    db.executescript("""
        CREATE TABLE provider_snapshots(account_id TEXT,conversation_id TEXT,snapshot_id TEXT,captured_at TEXT,data TEXT);
        CREATE TABLE provider_checks(account_id TEXT,conversation_id TEXT,kind TEXT,data TEXT);
    """)
    for account, title in [("private", "Anbieter & Söhne"), ("business", "OTHER-PROVIDER")]:
        put(db, "provider_snapshots", (account, "chat:1", "s1", "2026-11-01"), {"snapshot_id": "s1", "kind": "listing_imprint", "captured_at": "2026-11-01", "user_id": "456", "listing_id": "12345", "source_url": "https://www.kleinanzeigen.de/s-anzeige/12345", "title": title, "text": "Impressum\nArchivstraße 2", "truncated": False})
    put(db, "provider_checks", ("private", "chat:1", "listing_imprint"), {"kind": "listing_imprint", "status": "unavailable", "captured_at": "2026-11-04", "source_url": "javascript:alert(1)"})
    destination = exporter.export_archive(root, "private")
    data = json.loads((destination / "archive.json").read_text())
    context = data["conversations"][0]["provider_context"]
    assert context["snapshots"][0]["title"] == "Anbieter & Söhne"
    assert context["latest_checks"][0]["status"] == "unavailable"
    assert len(context["snapshots"]) == 1
    page = (destination / data["conversations"][0]["export_file"]).read_text()
    assert "Archivstraße 2" in page
    assert "Anbieter &amp; Söhne" in page
    assert 'href="javascript:' not in page
    assert "OTHER-PROVIDER" not in page


def test_system_events_are_separate_from_user_messages_and_account_scoped(archive):
    root, db, _ = archive
    db.execute("CREATE TABLE system_events(account_id TEXT,conversation_id TEXT,event_id TEXT,data TEXT)")
    put(db, "system_events", ("private", "chat:1", "event1"), {"event_id": "event1", "event_type": "AD_DELETED", "text": "Anzeige gelöscht", "previous_texts": ["Früherer Stand"]})
    put(db, "system_events", ("business", "chat:1", "event2"), {"event_id": "event2", "text": "OTHER-EVENT"})
    destination = exporter.export_archive(root, "private")
    data = json.loads((destination / "archive.json").read_text())
    conversation = data["conversations"][0]
    assert len(conversation["messages"]) == 1
    assert len(conversation["system_events"]) == 1
    assert conversation["system_events"][0]["event_id"] == "event1"
    assert "sender_user_id" not in conversation["system_events"][0]
    page = (destination / conversation["export_file"]).read_text()
    assert "Plattform-Systemereignisse" in page
    assert "Anzeige gelöscht" in page
    assert "Früherer Stand" in page
    assert "OTHER-EVENT" not in page
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["counts"]["system_events"] == 1


@pytest.mark.parametrize("account", ["../private", "/private", "", "bad?name", "unknown"])
def test_invalid_or_unknown_account_does_not_create_export(archive, account):
    root, _, _ = archive
    with pytest.raises(exporter.ExportError):
        exporter.export_archive(root, account)
    assert not (root / "exports").exists()


def test_existing_export_is_never_overwritten(archive, monkeypatch):
    root, _, _ = archive
    monkeypatch.setattr(exporter, "_export_name", lambda account: "fixed")
    first = exporter.export_archive(root, "private")
    original = (first / "manifest.json").read_bytes()
    with pytest.raises(exporter.ExportError, match="existiert bereits"):
        exporter.export_archive(root, "private")
    assert (first / "manifest.json").read_bytes() == original
    assert not list(first.parent.glob(".export-*"))


def test_atomic_publication_refuses_a_racing_empty_directory(archive, monkeypatch):
    root, _, _ = archive
    original = exporter._publish

    def race(staging, destination):
        destination.mkdir()
        original(staging, destination)

    monkeypatch.setattr(exporter, "_publish", race)
    with pytest.raises(exporter.ExportError, match="existiert bereits"):
        exporter.export_archive(root, "private")
    entries = list((root / "exports").iterdir())
    assert len(entries) == 1
    assert entries[0].is_dir()
    assert list(entries[0].iterdir()) == []
    assert not list((root / "exports").glob(".export-*"))


def test_failure_cleans_staging_and_never_publishes_partial_export(archive, monkeypatch):
    root, _, _ = archive
    original = exporter._write

    def fail_manifest(path, contents):
        if path.name == "manifest.json":
            raise OSError("simulated disk failure")
        original(path, contents)

    monkeypatch.setattr(exporter, "_write", fail_manifest)
    with pytest.raises(OSError, match="simulated disk failure"):
        exporter.export_archive(root, "private")
    assert list((root / "exports").iterdir()) == []


def test_summary_only_chat_is_not_presented_as_complete_history(archive):
    root, db, _ = archive
    put(db, "conversations", ("private", "summary"), {"conversation_id": "summary", "title": "Nur Übersicht"})
    destination = exporter.export_archive(root, "private")
    data = json.loads((destination / "archive.json").read_text())
    summary = next(item for item in data["conversations"] if item["conversation_id"] == "summary")
    assert "nur die Gesprächsübersicht archiviert" in (destination / summary["export_file"]).read_text()


def test_symbolic_export_destination_is_rejected(archive, tmp_path):
    root, _, _ = archive
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(exporter.ExportError, match="symbolischer Link"):
        exporter.export_archive(root, "private", link)
    assert list(target.iterdir()) == []
