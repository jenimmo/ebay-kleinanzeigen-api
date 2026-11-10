"""Sign and atomically install a Web-only update of the existing Safari app.

No Xcode invocation, temporary-app registration, process control or rights changes.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import struct
import subprocess
import tempfile

import install_safari_app as installer
import safari_icon_assets as icons


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "safari-extension/web"
ARCHIVE = ROOT / "build/safari/Kleinanzeigen Safari.zip"
SIGNING_IDENTITY = "92000E2CA24FCFF48E5C8988E25B0706B96E27C8"
WEB_FILES = frozenset({
    "manifest.json", "protocol.js", "background.js", "content.js",
    "content-bootstrap.js", "popup.html", "popup.js", "popup.css",
    "popup-bootstrap.js", "messaging-read.js",
})
ADDED_WEB_FILES = frozenset({"messaging-read.js"})
PREVIOUS_CONTENT_SCRIPTS = ["content-bootstrap.js", "protocol.js", "content.js"]
CONTENT_SCRIPTS = ["content-bootstrap.js", "protocol.js", "messaging-read.js", "content.js"]
PRESERVED = (
    "identifier,entitlements,requirements,flags,runtime,"
    "launch-constraints,library-constraints"
)


def run(arguments):
    return subprocess.run(arguments, check=True, capture_output=True)


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise installer.InstallError("Die Web-Version muss drei numerische Teile haben.")
    return tuple(map(int, value.split(".")))


def expected_entitlements(identifier):
    result = {
        "com.apple.application-identifier": installer.TEAM_ID + "." + identifier,
        "com.apple.developer.team-identifier": installer.TEAM_ID,
        "com.apple.security.app-sandbox": True,
        "com.apple.security.application-groups": [installer.TEAM_ID + "." + installer.APP_ID],
    }
    if identifier == installer.APP_ID:
        result["com.apple.security.files.user-selected.read-only"] = True
    else:
        result["com.apple.security.network.client"] = True
    return result


def signed_metadata(bundle, identifier):
    entitlements = plistlib.loads(run([
        "/usr/bin/codesign", "-d", "--entitlements", ":-", str(bundle)
    ]).stdout)
    if entitlements != expected_entitlements(identifier):
        raise installer.InstallError("Die signierten Rechte weichen vom geprüften Projekt ab.")
    details = run(["/usr/bin/codesign", "-dv", "--verbose=4", str(bundle)]).stderr.decode()
    flags = re.search(r"\bflags=(0x[0-9a-f]+)\(", details)
    runtime = re.search(r"^Runtime Version=(.+)$", details, re.MULTILINE)
    if flags is None or int(flags[1], 16) != 0x10000 or runtime is None:
        raise installer.InstallError("Die erwartete Hardened-Runtime-Signatur fehlt.")
    requirement = run(["/usr/bin/codesign", "-d", "-r-", str(bundle)]).stdout
    return entitlements, flags[1], runtime[1], requirement


def validate_profile(bundle):
    data = plistlib.loads(run([
        "/usr/bin/security", "cms", "-D", "-i",
        str(bundle / "Contents/embedded.provisionprofile")
    ]).stdout)
    certificates = {
        hashlib.sha1(value).hexdigest().upper()
        for value in data.get("DeveloperCertificates", [])
    }
    expiry = data.get("ExpirationDate")
    if (
        data.get("TeamIdentifier") != [installer.TEAM_ID]
        or SIGNING_IDENTITY not in certificates
        or not isinstance(expiry, datetime)
        or expiry.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
    ):
        raise installer.InstallError("Vorhandenes Projektprofil erlaubt die Signierung nicht.")


def native_payload(data):
    """Hash all arm64 Mach-O bytes except its signature and size-only bookkeeping.

    codesign changes the embedded signature, its length and __LINKEDIT sizes.
    Everything else, including all code/data sections and signature offset, stays.
    """
    if len(data) < 32:
        raise installer.InstallError("Native Mach-O-Datei ist unvollständig.")
    magic, cpu, _, _, count, size, _, _ = struct.unpack_from("<8I", data)
    if magic != 0xFEEDFACF or cpu != 0x0100000C or size > len(data) - 32:
        raise installer.InstallError("Nur das geprüfte arm64-Mach-O-Format ist erlaubt.")
    position, signature, masked = 32, None, []
    for _ in range(count):
        if position + 8 > 32 + size:
            raise installer.InstallError("Native Load-Commands sind ungültig.")
        command, length = struct.unpack_from("<II", data, position)
        if length < 8 or position + length > 32 + size:
            raise installer.InstallError("Native Load-Commands sind ungültig.")
        if command == 0x1D:
            if length != 16 or signature is not None:
                raise installer.InstallError("Native Signaturgrenze ist ungültig.")
            offset, signature_size = struct.unpack_from("<II", data, position + 8)
            signature = offset, signature_size
            masked.append((position + 12, 4))
        if command == 0x19 and length >= 72:
            if data[position + 8:position + 24].rstrip(b"\0") == b"__LINKEDIT":
                masked.extend(((position + 32, 8), (position + 48, 8)))
        position += length
    if (
        position != 32 + size or signature is None
        or signature[0] < position or signature[0] + signature[1] != len(data)
    ):
        raise installer.InstallError("Native Signaturgrenze ist ungültig.")
    payload = bytearray(data[:signature[0]])
    for offset, length in masked:
        payload[offset:offset + length] = b"\0" * length
    return hashlib.sha256(payload).digest()


def inventory():
    text = run([
        "/usr/bin/pluginkit", "-m", "-A", "-D", "-vv", "-i", installer.EXTENSION_ID
    ]).stdout.decode()
    records, version = [], None
    pattern = re.compile(re.escape(installer.EXTENSION_ID) + r"\(([^)]+)\)")
    for line in text.splitlines():
        match = pattern.search(line)
        if match:
            version = match[1]
        if line.strip().startswith("Path ="):
            if version is None:
                raise installer.InstallError("Plugin-Registrierung ist nicht eindeutig lesbar.")
            records.append((line.split("=", 1)[1].strip(), version))
    return records


def refresh_installed_registration(app, version):
    extension = str(app / installer.EXTENSION_PATH)
    records = inventory()
    if any(path != extension for path, _ in records):
        raise installer.InstallError("Eine zusätzliche Plugin-Installation muss zuerst geprüft werden.")
    if records != [(extension, version)]:
        run(["/usr/bin/pluginkit", "-a", extension])
    if inventory() != [(extension, version)]:
        raise installer.InstallError("Die permanente Plugin-Registrierung ist noch nicht aktuell.")


def validate_web_files(web, previous_manifest, version):
    paths = {entry.name for entry in web.iterdir() if entry.is_file()}
    if paths != WEB_FILES or any((web / name).is_symlink() for name in WEB_FILES):
        raise installer.InstallError("Die Web-Dateien weichen von der erlaubten Liste ab.")
    files = {name: (web / name).read_bytes() for name in WEB_FILES}
    manifest = json.loads(files["manifest.json"])
    if manifest.get("version") != version:
        raise installer.InstallError("Manifest- und App-Version stimmen nicht überein.")
    expected = {k: v for k, v in previous_manifest.items() if k != "version"}
    updated = {k: v for k, v in manifest.items() if k != "version"}
    if updated != expected:
        # The one reviewed migration adds our fixed local reader between the
        # protocol and existing content script. All other manifest fields stay.
        expected = copy.deepcopy(expected)
        scripts = expected.get("content_scripts")
        if (
            isinstance(scripts, list) and len(scripts) == 1
            and isinstance(scripts[0], dict)
            and scripts[0].get("js") == PREVIOUS_CONTENT_SCRIPTS
        ):
            scripts[0]["js"] = CONTENT_SCRIPTS.copy()
    if updated != expected:
        raise installer.InstallError("Web-only-Updates dürfen keine Manifestrechte ändern.")
    if icons.manifest_has_icons(manifest):
        # Subsequent Web-only updates keep the already installed icon contract.
        # First-time adoption uses update_safari_icons.py, which also supplies
        # the host ICNS and cannot change any JavaScript/native program bytes.
        files.update(icons.web_icon_files())
    return files


def update(version, build):
    app = installer._installed_bundle_path()
    if app.resolve() != app:
        raise installer.InstallError("Die installierte Basis darf kein symbolischer Pfad sein.")
    installer.verify_bundle(app)
    extension = app / installer.EXTENSION_PATH
    infos = [plistlib.loads((part / "Contents/Info.plist").read_bytes()) for part in (app, extension)]
    for info in infos:
        if version_tuple(version) <= version_tuple(info["CFBundleShortVersionString"]) or build <= int(info["CFBundleVersion"]):
            raise installer.InstallError("Version und Build müssen gegenüber der installierten Basis steigen.")
    if inventory() != [(str(extension), infos[1]["CFBundleShortVersionString"])]:
        raise installer.InstallError("Vor dem Update muss genau die permanente App registriert sein.")
    identities = run(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"]).stdout.decode()
    if SIGNING_IDENTITY not in identities:
        raise installer.InstallError("Die vorhandene freigegebene Signieridentität fehlt.")
    metadata = []
    for part, identifier in ((app, installer.APP_ID), (extension, installer.EXTENSION_ID)):
        validate_profile(part)
        metadata.append(signed_metadata(part, identifier))
    relative_extension = Path(installer.EXTENSION_PATH)
    resource_prefix = relative_extension / "Contents/Resources"
    web_files = validate_web_files(
        WEB, json.loads((app / resource_prefix / "manifest.json").read_text()), version
    )
    for name in icons.WEB_ICON_FILES & web_files.keys():
        if (app / resource_prefix / name).read_bytes() != web_files[name]:
            raise installer.InstallError("Iconänderungen benötigen das eigenständige Iconupdate.")
    baseline = installer._bundle_files(app)
    executables = [Path("Contents/MacOS") / infos[0]["CFBundleExecutable"],
                   relative_extension / "Contents/MacOS" / infos[1]["CFBundleExecutable"]]
    payloads = [native_payload((app / path).read_bytes()) for path in executables]
    stage = Path(tempfile.mkdtemp(prefix=".kleinanzeigen-webupdate-", dir="/private/tmp"))
    candidate = stage / app.name
    try:
        installer._run(["/usr/bin/ditto", "--norsrc", "--noextattr", str(app), str(candidate)])
        if installer._bundle_files(candidate) != baseline:
            raise installer.InstallError("Die private Basiskopie ist nicht identisch.")
        for name, data in web_files.items():
            (candidate / resource_prefix / name).write_bytes(data)
        for part in (candidate, candidate / relative_extension):
            path = part / "Contents/Info.plist"
            info = plistlib.loads(path.read_bytes())
            info.update(CFBundleShortVersionString=version, CFBundleVersion=str(build))
            path.write_bytes(plistlib.dumps(info))
        for part in (candidate / relative_extension, candidate):
            run(["/usr/bin/codesign", "--force", "--sign", SIGNING_IDENTITY,
                 "--timestamp", "--preserve-metadata=" + PRESERVED, str(part)])
        installer.verify_bundle(candidate)
        for index, (part, identifier) in enumerate(((candidate, installer.APP_ID), (candidate / relative_extension, installer.EXTENSION_ID))):
            if signed_metadata(part, identifier) != metadata[index]:
                raise installer.InstallError("Signierte Rechte oder Anforderungen wurden verändert.")
            if native_payload((candidate / executables[index]).read_bytes()) != payloads[index]:
                raise installer.InstallError("Native Programmbytes wurden verändert.")
        changed = {str(path) for path in executables} | {
            "Contents/Info.plist", "Contents/_CodeSignature/CodeResources",
            str(relative_extension / "Contents/Info.plist"),
            str(relative_extension / "Contents/_CodeSignature/CodeResources"),
        } | {str(resource_prefix / name) for name in web_files}
        final = installer._bundle_files(candidate)
        expected_files = set(baseline) | {
            str(resource_prefix / name) for name in ADDED_WEB_FILES
        }
        if set(final) != expected_files or any(
            digest != final[name] for name, digest in baseline.items() if name not in changed
        ):
            raise installer.InstallError("Das Update verändert Dateien außerhalb des erlaubten Umfangs.")
        archive = stage / ARCHIVE.name
        run(["/usr/bin/ditto", "-c", "-k", "--norsrc", "--noextattr", "--keepParent", str(candidate), str(archive)])
        installer._verify_archive(archive, candidate, final)
        # Abort if another update changed the installed base meanwhile.
        if installer._bundle_files(app) != baseline:
            raise installer.InstallError("Die installierte Basis wurde zwischenzeitlich geändert.")
        installer.install(candidate, app)
        ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
        archive_copy = ARCHIVE.with_suffix(".zip.tmp")
        shutil.copyfile(archive, archive_copy)
        os.replace(archive_copy, ARCHIVE)
        refresh_installed_registration(app, version)
    finally:
        shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build", type=int, required=True)
    args = parser.parse_args()
    try:
        update(args.version, args.build)
    except (installer.InstallError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Web-only-Update abgebrochen: {exc}") from None
    print(f"Installed Web-only update {args.version} build {args.build}: {installer._installed_bundle_path()}")


if __name__ == "__main__":
    main()
