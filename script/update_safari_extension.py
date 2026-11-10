"""Rebuild only the native Safari extension and atomically update its existing app.

The Xcode target has no host dependency, so no cache .app is built or registered.
The installed host code, provisioning profiles and entitlements remain unchanged.
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
import update_safari_web as web_update


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "safari-extension/native/Kleinanzeigen Safari/Kleinanzeigen Safari.xcodeproj"
TARGET = "Kleinanzeigen Safari Extension"


def validate_target(project):
    objects = project.get("objects", {})
    targets = [
        value for value in objects.values()
        if isinstance(value, dict) and value.get("isa") == "PBXNativeTarget"
        and value.get("name") == TARGET
    ]
    if (
        len(targets) != 1
        or targets[0].get("productType") != "com.apple.product-type.app-extension"
        or targets[0].get("dependencies") != []
        or any(
            objects.get(phase, {}).get("isa") == "PBXShellScriptBuildPhase"
            for phase in targets[0].get("buildPhases", [])
        )
    ):
        raise installer.InstallError("Der native Build muss ausschließlich den geprüften Extension-Target erzeugen.")


def build_arguments(stage, version, build):
    # A target (not the host scheme) deliberately avoids ApplicationProductTypeSpec
    # and its RegisterWithLaunchServices task. The existing profile is Xcode
    # managed, so Xcode requires Automatic + Apple Development. No provisioning
    # updates are permitted; profile bytes and signed metadata must match the
    # installed extension before the pinned final re-signing step.
    return [
        "/usr/bin/xcodebuild", "-quiet", "-project", str(PROJECT),
        "-target", TARGET, "-configuration", "Debug", "-sdk", "macosx",
        "ARCHS=arm64", "ONLY_ACTIVE_ARCH=YES",
        "SYMROOT=" + str(stage / "products"),
        "OBJROOT=" + str(stage / "intermediates"),
        "CODE_SIGN_STYLE=Automatic", "CODE_SIGN_IDENTITY=Apple Development",
        "DEVELOPMENT_TEAM=" + installer.TEAM_ID,
        "CODE_SIGN_INJECT_BASE_ENTITLEMENTS=NO",
        "MARKETING_VERSION=" + version, "CURRENT_PROJECT_VERSION=" + str(build),
        "build",
    ]


def validate_host_changes(before, after, host_executable):
    extension_prefix = installer.EXTENSION_PATH + "/"
    before_host = {name: digest for name, digest in before.items() if not name.startswith(extension_prefix)}
    after_host = {name: digest for name, digest in after.items() if not name.startswith(extension_prefix)}
    allowed = {host_executable, "Contents/Info.plist", "Contents/_CodeSignature/CodeResources"}
    if set(before_host) != set(after_host) or any(
        digest != after_host[name] for name, digest in before_host.items() if name not in allowed
    ):
        raise installer.InstallError("Native Extension-Updates dürfen keine weiteren Host-Dateien ändern.")


def cleanup_stage(stage):
    # Preserve a stage if macOS unexpectedly discovered or started any of it.
    # Never delete an executing bundle or unregister an unexamined path here.
    try:
        registered = web_update.inventory()
        processes = web_update.run(["/bin/ps", "-axo", "comm="]).stdout.decode().splitlines()
        if any(Path(path).is_relative_to(stage) for path, _ in registered) or any(
            command.strip().startswith(str(stage) + "/") for command in processes
        ):
            raise installer.InstallError("Eine unerwartet aktive Buildkopie bleibt zur Prüfung unter " + str(stage))
    except (OSError, subprocess.CalledProcessError) as exc:
        raise installer.InstallError("Der private Buildordner konnte nicht sicher geprüft werden: " + str(stage)) from exc
    shutil.rmtree(stage)


def update(version, build):
    os.environ.setdefault("DEVELOPER_DIR", "/Applications/Xcode.app/Contents/Developer")
    app = installer._installed_bundle_path()
    if app.resolve() != app:
        raise installer.InstallError("Die installierte Basis darf kein symbolischer Pfad sein.")
    installer.verify_bundle(app)
    extension = app / installer.EXTENSION_PATH
    infos = [plistlib.loads((part / "Contents/Info.plist").read_bytes()) for part in (app, extension)]
    for info in infos:
        if web_update.version_tuple(version) <= web_update.version_tuple(info["CFBundleShortVersionString"]) or build <= int(info["CFBundleVersion"]):
            raise installer.InstallError("Version und Build müssen gegenüber der installierten Basis steigen.")
    registration = [(str(extension), infos[1]["CFBundleShortVersionString"])]
    if web_update.inventory() != registration:
        raise installer.InstallError("Vor dem Update muss genau die permanente App registriert sein.")
    project = json.loads(web_update.run([
        "/usr/bin/plutil", "-convert", "json", "-o", "-", str(PROJECT / "project.pbxproj")
    ]).stdout)
    validate_target(project)
    identities = web_update.run(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"]).stdout.decode()
    if web_update.SIGNING_IDENTITY not in identities:
        raise installer.InstallError("Die vorhandene freigegebene Signieridentität fehlt.")
    metadata = []
    for part, identifier in ((app, installer.APP_ID), (extension, installer.EXTENSION_ID)):
        web_update.validate_profile(part)
        metadata.append(web_update.signed_metadata(part, identifier))
    profile_path = Path("Contents/embedded.provisionprofile")
    resources_path = Path(installer.EXTENSION_PATH) / "Contents/Resources"
    web_files = web_update.validate_web_files(
        web_update.WEB, json.loads((app / resources_path / "manifest.json").read_text()), version
    )
    for name in web_update.icons.WEB_ICON_FILES & web_files.keys():
        if (app / resources_path / name).read_bytes() != web_files[name]:
            raise installer.InstallError("Iconänderungen benötigen das eigenständige Iconupdate.")
    baseline = installer._bundle_files(app)
    host_executable = "Contents/MacOS/" + infos[0]["CFBundleExecutable"]
    host_payload = web_update.native_payload((app / host_executable).read_bytes())
    stage = Path(tempfile.mkdtemp(prefix=".kleinanzeigen-extension-update-", dir="/private/tmp"))
    try:
        web_update.run(build_arguments(stage, version, build))
        if list(stage.rglob("*.app")) or web_update.inventory() != registration:
            raise installer.InstallError("Der Extension-Build hat unerwartet eine App oder Registrierung erzeugt.")
        built = stage / "products/Debug" / (TARGET + ".appex")
        if installer._bundle_id(built) != installer.EXTENSION_ID:
            raise installer.InstallError("Der Build lieferte keine passende native Erweiterung.")
        built_info = plistlib.loads((built / "Contents/Info.plist").read_bytes())
        if (built_info.get("CFBundleShortVersionString"), built_info.get("CFBundleVersion")) != (version, str(build)):
            raise installer.InstallError("Die native Erweiterung hat eine falsche Version.")
        web_update.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(built)])
        if (
            web_update.signed_metadata(built, installer.EXTENSION_ID) != metadata[1]
            or (built / profile_path).read_bytes() != (extension / profile_path).read_bytes()
        ):
            raise installer.InstallError("Native Rechte, Anforderungen oder das vorhandene Profil wurden verändert.")
        candidate = stage / app.name
        web_update.run(["/usr/bin/ditto", "--norsrc", "--noextattr", str(app), str(candidate)])
        if installer._bundle_files(candidate) != baseline:
            raise installer.InstallError("Die private Host-Basiskopie ist nicht identisch.")
        candidate_extension = candidate / installer.EXTENSION_PATH
        shutil.rmtree(candidate_extension)
        web_update.run(["/usr/bin/ditto", "--norsrc", "--noextattr", str(built), str(candidate_extension)])
        resources = candidate / resources_path
        # Replace only our fixed Web files; never include stale Xcode resources.
        if any(not path.is_file() or path.is_symlink() or path.name not in web_files for path in resources.iterdir()):
            raise installer.InstallError("Die native Erweiterung enthält unbekannte Webressourcen.")
        for name, data in web_files.items():
            (resources / name).write_bytes(data)
        if {path.name for path in resources.iterdir()} != set(web_files):
            raise installer.InstallError("Die Webressourcen sind unvollständig.")
        host_info = candidate / "Contents/Info.plist"
        info = plistlib.loads(host_info.read_bytes())
        info.update(CFBundleShortVersionString=version, CFBundleVersion=str(build))
        host_info.write_bytes(plistlib.dumps(info))
        for part in (candidate_extension, candidate):
            web_update.run(["/usr/bin/codesign", "--force", "--sign", web_update.SIGNING_IDENTITY,
                            "--timestamp", "--preserve-metadata=" + web_update.PRESERVED, str(part)])
        installer.verify_bundle(candidate)
        for index, (part, identifier) in enumerate(((candidate, installer.APP_ID), (candidate_extension, installer.EXTENSION_ID))):
            if web_update.signed_metadata(part, identifier) != metadata[index]:
                raise installer.InstallError("Die signierten Rechte oder Anforderungen wurden verändert.")
        if web_update.native_payload((candidate / host_executable).read_bytes()) != host_payload:
            raise installer.InstallError("Der Host-Programmcode wurde verändert.")
        final = installer._bundle_files(candidate)
        validate_host_changes(baseline, final, host_executable)
        archive = stage / web_update.ARCHIVE.name
        web_update.run(["/usr/bin/ditto", "-c", "-k", "--norsrc", "--noextattr", "--keepParent", str(candidate), str(archive)])
        installer._verify_archive(archive, candidate, final)
        if installer._bundle_files(app) != baseline or web_update.inventory() != registration:
            raise installer.InstallError("Die installierte Basis oder Registrierung wurde zwischenzeitlich geändert.")
        installer.install(candidate, app)
        web_update.ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
        archive_copy = web_update.ARCHIVE.with_suffix(".zip.tmp")
        shutil.copyfile(archive, archive_copy)
        os.replace(archive_copy, web_update.ARCHIVE)
        web_update.refresh_installed_registration(app, version)
    finally:
        cleanup_stage(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build", type=int, required=True)
    args = parser.parse_args()
    try:
        update(args.version, args.build)
    except (installer.InstallError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Native Extension-Update abgebrochen: {exc}") from None
    print(f"Installed native extension update {args.version} build {args.build}: {installer._installed_bundle_path()}")


if __name__ == "__main__":
    main()
