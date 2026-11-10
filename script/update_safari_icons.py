"""Install only the fixed app/extension icons, manifest references and versions.

No browser or host process control, no Xcode build, no session/profile changes.
The existing native program and all JavaScript bytes are preserved exactly.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

import install_safari_app as installer
import safari_icon_assets as icons
import update_safari_web as web


def update(version, build):
    app = installer._installed_bundle_path()
    if app.resolve() != app:
        raise installer.InstallError("Die installierte Basis darf kein symbolischer Pfad sein.")
    installer.verify_bundle(app)
    relative_extension = Path(installer.EXTENSION_PATH)
    extension = app / relative_extension
    resources = relative_extension / "Contents/Resources"
    infos = [plistlib.loads((part / "Contents/Info.plist").read_bytes()) for part in (app, extension)]
    for info in infos:
        if web.version_tuple(version) <= web.version_tuple(info["CFBundleShortVersionString"]) or build <= int(info["CFBundleVersion"]):
            raise installer.InstallError("Version und Build müssen gegenüber der installierten Basis steigen.")
    if infos[0].get("CFBundleIconFile") not in {None, icons.APP_ICON_NAME}:
        raise installer.InstallError("Die installierte App hat eine unerwartete Iconzuordnung.")
    registration = [(str(extension), infos[1]["CFBundleShortVersionString"])]
    if web.inventory() != registration:
        raise installer.InstallError("Vor dem Update muss genau die permanente App registriert sein.")
    old_manifest = json.loads((app / resources / "manifest.json").read_text())
    manifest_path = web.WEB / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise installer.InstallError("Das lokale Manifest muss eine reguläre Projektdatei sein.")
    manifest_bytes = manifest_path.read_bytes()
    source_manifest = json.loads(manifest_bytes)
    if source_manifest != icons.icon_manifest(old_manifest, version):
        raise installer.InstallError("Das Iconupdate darf nur Version und festgelegte Iconreferenzen ändern.")
    # A resource update must not silently install a concurrent parser change.
    for name in web.WEB_FILES - {"manifest.json"}:
        if (web.WEB / name).is_symlink() or (web.WEB / name).read_bytes() != (app / resources / name).read_bytes():
            raise installer.InstallError("Webcode wurde verändert; zuerst das eigenständige Webupdate installieren.")
    image_files = icons.web_icon_files()
    native_icon = icons.app_icon()
    identities = web.run(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"]).stdout.decode()
    if web.SIGNING_IDENTITY not in identities:
        raise installer.InstallError("Die vorhandene freigegebene Signieridentität fehlt.")
    metadata = []
    for part, identifier in ((app, installer.APP_ID), (extension, installer.EXTENSION_ID)):
        web.validate_profile(part)
        metadata.append(web.signed_metadata(part, identifier))
    baseline = installer._bundle_files(app)
    executables = [Path("Contents/MacOS") / infos[0]["CFBundleExecutable"], relative_extension / "Contents/MacOS" / infos[1]["CFBundleExecutable"]]
    payloads = [web.native_payload((app / path).read_bytes()) for path in executables]
    stage = Path(tempfile.mkdtemp(prefix=".kleinanzeigen-iconupdate-", dir="/private/tmp"))
    candidate = stage / app.name
    try:
        installer._run(["/usr/bin/ditto", "--norsrc", "--noextattr", str(app), str(candidate)])
        if installer._bundle_files(candidate) != baseline:
            raise installer.InstallError("Die private Basiskopie ist nicht identisch.")
        for name, data in image_files.items():
            (candidate / resources / name).write_bytes(data)
        (candidate / resources / "manifest.json").write_bytes(manifest_bytes)
        (candidate / icons.APP_ICON_PATH).parent.mkdir(parents=True, exist_ok=True)
        (candidate / icons.APP_ICON_PATH).write_bytes(native_icon)
        for index, part in enumerate((candidate, candidate / relative_extension)):
            path = part / "Contents/Info.plist"
            info = plistlib.loads(path.read_bytes())
            info.update(CFBundleShortVersionString=version, CFBundleVersion=str(build))
            if index == 0:
                info["CFBundleIconFile"] = icons.APP_ICON_NAME
            path.write_bytes(plistlib.dumps(info))
        for part in (candidate / relative_extension, candidate):
            web.run(["/usr/bin/codesign", "--force", "--sign", web.SIGNING_IDENTITY, "--timestamp", "--preserve-metadata=" + web.PRESERVED, str(part)])
        installer.verify_bundle(candidate)
        for index, (part, identifier) in enumerate(((candidate, installer.APP_ID), (candidate / relative_extension, installer.EXTENSION_ID))):
            if web.signed_metadata(part, identifier) != metadata[index] or web.native_payload((candidate / executables[index]).read_bytes()) != payloads[index]:
                raise installer.InstallError("Native Programmbytes, Rechte oder Anforderungen wurden verändert.")
        added = {str(resources / name) for name in icons.WEB_ICON_FILES} | {icons.APP_ICON_PATH}
        changed = added | {str(path) for path in executables} | {
            str(resources / "manifest.json"), "Contents/Info.plist", "Contents/_CodeSignature/CodeResources",
            str(relative_extension / "Contents/Info.plist"), str(relative_extension / "Contents/_CodeSignature/CodeResources"),
        }
        final = installer._bundle_files(candidate)
        if set(final) != set(baseline) | added or any(digest != final[name] for name, digest in baseline.items() if name not in changed):
            raise installer.InstallError("Das Iconupdate verändert Dateien außerhalb des erlaubten Umfangs.")
        archive = stage / web.ARCHIVE.name
        web.run(["/usr/bin/ditto", "-c", "-k", "--norsrc", "--noextattr", "--keepParent", str(candidate), str(archive)])
        installer._verify_archive(archive, candidate, final)
        if installer._bundle_files(app) != baseline or web.inventory() != registration:
            raise installer.InstallError("Die installierte Basis oder Registrierung wurde zwischenzeitlich geändert.")
        installer.install(candidate, app)
        web.ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
        archive_copy = web.ARCHIVE.with_suffix(".zip.tmp")
        shutil.copyfile(archive, archive_copy)
        os.replace(archive_copy, web.ARCHIVE)
        web.refresh_installed_registration(app, version)
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
        raise SystemExit(f"Iconupdate abgebrochen: {exc}") from None
    print(f"Installed icon update {args.version} build {args.build}: {installer._installed_bundle_path()}")


if __name__ == "__main__":
    main()
