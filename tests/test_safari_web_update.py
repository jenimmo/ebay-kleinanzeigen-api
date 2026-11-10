"""Web update guards using temporary bundles; never signs or installs a real app."""

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "script"
sys.path.insert(0, str(SCRIPT))
try:
    spec = importlib.util.spec_from_file_location("safari_web_update", SCRIPT / "update_safari_web.py")
    update = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(update)
finally:
    sys.path.pop(0)


def macho(signature=b"sign"):
    header = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 2, 88, 0, 0)
    segment = struct.pack("<II16s4Q4I", 0x19, 72, b"__LINKEDIT", 0, 4096, 120, 20, 1, 1, 0, 0)
    signing = struct.pack("<4I", 0x1D, 16, 136, len(signature))
    return header + segment + signing + b"native-program!!" + signature


def test_native_payload_allows_only_signature_and_its_size_bookkeeping():
    original = macho()
    signed = bytearray(macho(b"new-signature-longer"))
    struct.pack_into("<Q", signed, 32 + 32, 8192)
    struct.pack_into("<Q", signed, 32 + 48, 35)
    assert update.native_payload(original) == update.native_payload(signed)
    signed[121] ^= 1
    assert update.native_payload(original) != update.native_payload(signed)


def test_native_payload_rejects_signature_offset_move_and_malformed_commands():
    original = bytearray(macho())
    struct.pack_into("<I", original, 32 + 72 + 8, 135)
    with pytest.raises(update.installer.InstallError, match="Signaturgrenze"):
        update.native_payload(original)
    struct.pack_into("<I", original, 32 + 4, 0)
    with pytest.raises(update.installer.InstallError, match="Load-Commands"):
        update.native_payload(original)


@pytest.fixture
def web_files(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    previous = {
        "manifest_version": 2, "version": "1.0.4", "permissions": ["nativeMessaging"],
        "content_scripts": [{"matches": ["https://www.kleinanzeigen.de/*"],
                             "js": update.PREVIOUS_CONTENT_SCRIPTS.copy()}],
    }
    for name in update.WEB_FILES:
        (web / name).write_text("script or static content")
    manifest = json.loads(json.dumps(previous))
    manifest["version"] = "1.0.5"
    manifest["content_scripts"][0]["js"] = update.CONTENT_SCRIPTS.copy()
    (web / "manifest.json").write_text(json.dumps(manifest))
    return web, previous


def test_web_update_rejects_new_permissions_and_extra_files(web_files):
    web, previous = web_files
    assert len(update.validate_web_files(web, previous, "1.0.5")) == 10
    manifest = json.loads((web / "manifest.json").read_text())
    manifest["permissions"].append("<all_urls>")
    (web / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(update.installer.InstallError, match="Manifestrechte"):
        update.validate_web_files(web, previous, "1.0.5")
    (web / "unexpected.js").write_text("extra")
    with pytest.raises(update.installer.InstallError, match="erlaubten Liste"):
        update.validate_web_files(web, previous, "1.0.5")


def test_web_update_rejects_symlink_source(web_files, tmp_path):
    web, previous = web_files
    (web / "content.js").unlink()
    outside = tmp_path / "outside.js"
    outside.write_text("external file")
    (web / "content.js").symlink_to(outside)
    with pytest.raises(update.installer.InstallError, match="erlaubten Liste"):
        update.validate_web_files(web, previous, "1.0.5")


@pytest.mark.parametrize("mutation", ["order", "additional_script", "matches", "background"])
def test_reader_manifest_migration_rejects_other_changes(web_files, mutation):
    web, previous = web_files
    manifest = json.loads((web / "manifest.json").read_text())
    scripts = manifest["content_scripts"][0]
    if mutation == "order":
        scripts["js"] = ["messaging-read.js", "content-bootstrap.js", "protocol.js", "content.js"]
    elif mutation == "additional_script":
        scripts["js"].append("another-reader.js")
    elif mutation == "matches":
        scripts["matches"].append("https://example.org/*")
    else:
        manifest["background"] = {"scripts": ["messaging-read.js"]}
    (web / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(update.installer.InstallError, match="Manifestrechte"):
        update.validate_web_files(web, previous, "1.0.5")


def test_reader_manifest_migration_is_stable_and_cannot_be_reversed(web_files):
    web, previous = web_files
    update.validate_web_files(web, previous, "1.0.5")
    migrated = json.loads((web / "manifest.json").read_text())
    update.validate_web_files(web, migrated, "1.0.5")
    (web / "manifest.json").write_text(json.dumps({**previous, "version": "1.0.5"}))
    with pytest.raises(update.installer.InstallError, match="Manifestrechte"):
        update.validate_web_files(web, migrated, "1.0.5")


def test_profile_requires_existing_allowed_certificate(monkeypatch, tmp_path):
    certificate = b"public certificate fixture"
    monkeypatch.setattr(update, "SIGNING_IDENTITY", hashlib.sha1(certificate).hexdigest().upper())
    profile = {
        "TeamIdentifier": [update.installer.TEAM_ID],
        "DeveloperCertificates": [certificate],
        "ExpirationDate": (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None),
    }
    monkeypatch.setattr(update, "run", lambda args: subprocess.CompletedProcess(args, 0, plistlib.dumps(profile), b""))
    update.validate_profile(tmp_path)
    profile["DeveloperCertificates"] = [b"another certificate"]
    with pytest.raises(update.installer.InstallError, match="Projektprofil"):
        update.validate_profile(tmp_path)
    profile["DeveloperCertificates"] = [certificate]
    profile["ExpirationDate"] = datetime(2000, 1, 1)
    with pytest.raises(update.installer.InstallError, match="Projektprofil"):
        update.validate_profile(tmp_path)


def test_registration_refresh_is_conditional_and_never_unregisters(monkeypatch, tmp_path):
    app = tmp_path / "App.app"
    extension = str(app / update.installer.EXTENSION_PATH)
    state = [(extension, "1.0.4")]
    calls = []
    monkeypatch.setattr(update, "inventory", lambda: state)

    def run(args):
        calls.append(args)
        state[:] = [(extension, "1.0.5")]

    monkeypatch.setattr(update, "run", run)
    update.refresh_installed_registration(app, "1.0.5")
    update.refresh_installed_registration(app, "1.0.5")
    assert calls == [["/usr/bin/pluginkit", "-a", extension]]
    state.append(("/unrelated/Extension.appex", "1.0.5"))
    with pytest.raises(update.installer.InstallError, match="zusätzliche"):
        update.refresh_installed_registration(app, "1.0.5")
    assert len(calls) == 1


@pytest.fixture
def update_fixture(tmp_path, web_files, monkeypatch):
    web, previous = web_files
    app = tmp_path / "Applications/Kleinanzeigen Safari.app"
    extension = app / update.installer.EXTENSION_PATH
    for part, identifier, executable in ((app, update.installer.APP_ID, "Host"), (extension, update.installer.EXTENSION_ID, "Extension")):
        (part / "Contents/MacOS").mkdir(parents=True)
        (part / "Contents/_CodeSignature").mkdir()
        (part / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": identifier, "CFBundleExecutable": executable,
            "CFBundleShortVersionString": "1.0.4", "CFBundleVersion": "12",
        }))
        (part / "Contents/MacOS" / executable).write_bytes(macho())
        (part / "Contents/MacOS/Unchanged.debug.dylib").write_bytes(b"native dylib")
        (part / "Contents/embedded.provisionprofile").write_bytes(b"unchanged profile")
        (part / "Contents/_CodeSignature/CodeResources").write_bytes(b"old resources")
    resources = extension / "Contents/Resources"
    resources.mkdir()
    for name in update.WEB_FILES:
        shutil.copyfile(web / name, resources / name)
    (resources / "messaging-read.js").unlink()
    (resources / "manifest.json").write_text(json.dumps(previous))
    archive = tmp_path / "deliverable.zip"
    state = {"signs": [], "install_count": 0, "corrupt": False, "rights": False}
    monkeypatch.setattr(update, "WEB", web)
    monkeypatch.setattr(update, "ARCHIVE", archive)
    monkeypatch.setattr(update.installer, "_installed_bundle_path", lambda: app)
    monkeypatch.setattr(update.installer, "verify_bundle", lambda part: None)
    monkeypatch.setattr(update, "validate_profile", lambda part: None)
    monkeypatch.setattr(update, "inventory", lambda: [(str(extension), "1.0.4")])
    monkeypatch.setattr(update, "refresh_installed_registration", lambda *args: None)

    def metadata(part, identifier):
        if state["rights"] and part != app and part != extension:
            return "changed entitlements"
        return identifier

    monkeypatch.setattr(update, "signed_metadata", metadata)
    stage = tmp_path / "private-stage"

    def new_stage(**kwargs):
        stage.mkdir()
        return str(stage)

    monkeypatch.setattr(update.tempfile, "mkdtemp", new_stage)

    def copy(args):
        shutil.copytree(args[-2], args[-1])

    monkeypatch.setattr(update.installer, "_run", copy)

    def command(args):
        if args[0] == "/usr/bin/security":
            return subprocess.CompletedProcess(args, 0, update.SIGNING_IDENTITY.encode(), b"")
        if args[0] == "/usr/bin/codesign":
            state["signs"].append(args)
            part = Path(args[-1])
            info = plistlib.loads((part / "Contents/Info.plist").read_bytes())
            executable = part / "Contents/MacOS" / info["CFBundleExecutable"]
            data = bytearray(macho(b"new-signature"))
            if state["corrupt"]:
                data[121] ^= 1
            executable.write_bytes(data)
            (part / "Contents/_CodeSignature/CodeResources").write_bytes(b"new resources")
        elif args[0] == "/usr/bin/ditto":
            source, target = Path(args[-2]), Path(args[-1])
            with zipfile.ZipFile(target, "w") as saved:
                for entry in source.rglob("*"):
                    if entry.is_file():
                        saved.write(entry, source.name + "/" + entry.relative_to(source).as_posix())
        else:
            raise AssertionError("No Xcode, process control or cache registration is allowed")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(update, "run", command)

    def install(source, target):
        state["install_count"] += 1
        shutil.rmtree(target)
        shutil.copytree(source, target)

    monkeypatch.setattr(update.installer, "install", install)
    return app, archive, state, stage


def test_web_only_update_preserves_native_code_profiles_and_signs_inside_out(update_fixture):
    app, archive, state, stage = update_fixture
    before = update.installer._bundle_files(app)
    update.update("1.0.5", 13)
    assert state["install_count"] == 1 and archive.is_file() and not stage.exists()
    assert (app / update.installer.EXTENSION_PATH / "Contents/Resources/messaging-read.js").is_file()
    assert state["signs"][0][-1].endswith(".appex")
    assert state["signs"][1][-1].endswith(".app")
    assert all("--deep" not in command for command in state["signs"])
    assert all("--preserve-metadata=" + update.PRESERVED in command for command in state["signs"])
    after = update.installer._bundle_files(app)
    for name in before:
        if name.endswith((".dylib", ".provisionprofile")):
            assert before[name] == after[name]
    assert plistlib.loads((app / "Contents/Info.plist").read_bytes())["CFBundleVersion"] == "13"


@pytest.mark.parametrize("corruption", ["corrupt", "rights"])
def test_web_only_update_rejects_native_or_entitlement_change_before_install(update_fixture, corruption):
    app, archive, state, stage = update_fixture
    before = update.installer._bundle_files(app)
    state[corruption] = True
    with pytest.raises(update.installer.InstallError, match="verändert"):
        update.update("1.0.5", 13)
    assert update.installer._bundle_files(app) == before
    assert state["install_count"] == 0 and not archive.exists() and not stage.exists()


def test_web_only_update_rejects_version_reuse_before_signing(update_fixture):
    app, archive, state, stage = update_fixture
    with pytest.raises(update.installer.InstallError, match="steigen"):
        update.update("1.0.4", 13)
    assert not state["signs"] and not stage.exists() and not archive.exists()
