"""Install only our signed Safari bundle, with an atomic directory exchange.

Only a verified, unused generated cache copy may be removed from PlugInKit.
The installed app's registration/election, Safari settings and processes stay intact.
Running extension generations may still need to be reloaded by Safari itself.
"""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import zipfile


APP_ID = "de.matthias.kleinanzeigen.safari"
EXTENSION_ID = APP_ID + ".Extension"
TEAM_ID = "EUXB3Z2QNQ"
EXTENSION_PATH = "Contents/PlugIns/Kleinanzeigen Safari Extension.appex"


class InstallError(RuntimeError):
    pass


def _run(arguments):
    return subprocess.run(arguments, check=True, capture_output=True, text=True)


def _bundle_id(bundle):
    info = bundle / "Contents/Info.plist"
    if bundle.is_symlink() or not bundle.is_dir() or info.is_symlink():
        raise InstallError("App-Pfad oder Metadaten sind kein reguläres Bundle.")
    try:
        with info.open("rb") as stream:
            metadata = plistlib.load(stream)
        if not isinstance(metadata, dict):
            raise ValueError("bundle metadata is not a dictionary")
        return metadata.get("CFBundleIdentifier")
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise InstallError("Bundle-Metadaten sind ungültig.") from exc


def verify_bundle(bundle):
    extension = bundle / EXTENSION_PATH
    if _bundle_id(bundle) != APP_ID or _bundle_id(extension) != EXTENSION_ID:
        raise InstallError("Eine fremde App oder Erweiterung wird nicht ersetzt.")
    try:
        _run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)])
        for component in (bundle, extension):
            result = _run(
                ["/usr/bin/codesign", "-dv", "--verbose=4", str(component)]
            )
            lines = (result.stdout + result.stderr).splitlines()
            if f"TeamIdentifier={TEAM_ID}" not in lines:
                raise InstallError("Das signierte Bundle gehört nicht zum Projektteam.")
    except subprocess.CalledProcessError as exc:
        raise InstallError("Die Bundle-Signatur ist ungültig.") from exc


def _rename(source, destination, *, swap):
    if sys.platform != "darwin":
        raise InstallError("Die atomare Safari-Installation benötigt macOS.")
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # Public Darwin rename(2): RENAME_SWAP=2, RENAME_EXCL=4.
    # No non-atomic fallback: an unsupported filesystem leaves the old app intact.
    if rename(os.fsencode(source), os.fsencode(destination), 2 if swap else 4):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def _identity(path):
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_mtime_ns


def install(source, destination):
    source = Path(source).absolute()
    destination = Path(destination).absolute()
    if (
        source == destination
        or source in destination.parents
        or destination in source.parents
        or destination.is_symlink()
        or destination.parent.is_symlink()
    ):
        raise InstallError("Quell- und Zielpfade sind nicht unabhängig.")
    verify_bundle(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.parent / ".kleinanzeigen-safari-install.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError("Eine Safari-Installation läuft bereits.") from exc
        previous = None
        if destination.exists():
            verify_bundle(destination)
            previous = _identity(destination)
        stage = Path(
            tempfile.mkdtemp(prefix=".kleinanzeigen-safari-update-", dir=destination.parent)
        )
        candidate = stage / destination.name
        preserve_stage = False
        try:
            _run(
                ["/usr/bin/ditto", "--norsrc", "--noextattr", str(source), str(candidate)]
            )
            verify_bundle(candidate)
            if destination.is_symlink() or (
                previous is not None
                and (not destination.exists() or _identity(destination) != previous)
            ):
                raise InstallError("Das Installationsziel wurde zwischenzeitlich geändert.")
            _rename(candidate, destination, swap=previous is not None)
            try:
                verify_bundle(destination)
            except Exception:
                # The old complete bundle remains at candidate after RENAME_SWAP.
                # Restore it before cleanup if post-install verification fails.
                try:
                    _rename(destination, candidate, swap=previous is not None)
                except OSError as exc:
                    preserve_stage = True
                    raise InstallError(
                        f"Rücknahme fehlgeschlagen; vollständige Kopie bleibt unter {stage}."
                    ) from exc
                raise
        finally:
            if not preserve_stage:
                shutil.rmtree(stage)


def _generated_bundle_path():
    return Path(
        f"/private/tmp/kleinanzeigen-safari-build-{os.getuid()}/"
        "Build/Products/Debug/Kleinanzeigen Safari.app"
    )


def _installed_bundle_path():
    return Path.home() / "Applications/Kleinanzeigen Safari.app"


def _bundle_files(bundle):
    files = {}
    for entry in bundle.rglob("*"):
        if entry.is_symlink():
            raise InstallError("Cache-Bereinigung lehnt symbolische Bundle-Pfade ab.")
        if entry.is_file():
            files[entry.relative_to(bundle).as_posix()] = hashlib.sha256(
                entry.read_bytes()
            ).digest()
    return files


def _verify_archive(archive, source, expected_files):
    if archive.is_symlink() or not archive.is_file():
        raise InstallError("Das signierte Sicherungsarchiv fehlt.")
    try:
        with zipfile.ZipFile(archive) as saved:
            members = [entry for entry in saved.infolist() if not entry.is_dir()]
            names = [entry.filename for entry in members]
            prefix = source.name + "/"
            if len(set(names)) != len(names) or set(names) != {
                prefix + name for name in expected_files
            }:
                raise InstallError("Das Sicherungsarchiv enthält nicht genau das Bundle.")
            for name, expected in expected_files.items():
                if hashlib.sha256(saved.read(prefix + name)).digest() != expected:
                    raise InstallError("Das Sicherungsarchiv stimmt nicht mit dem Bundle überein.")
    except (OSError, zipfile.BadZipFile) as exc:
        raise InstallError("Das Sicherungsarchiv ist ungültig.") from exc


def cleanup_generated_bundle(source, destination, archive):
    """Remove only the archived build product after a verified installation."""
    source = Path(source).absolute()
    destination = Path(destination).absolute()
    archive = Path(archive).absolute()
    if (
        source != _generated_bundle_path()
        or source.resolve() != source
        or destination != _installed_bundle_path()
        or destination.resolve() != destination
    ):
        raise InstallError("Cache-Bereinigung ist nur für die festen Projektpfade erlaubt.")
    verify_bundle(source)
    verify_bundle(destination)
    files = _bundle_files(source)
    if files != _bundle_files(destination):
        raise InstallError("Die installierte App stimmt nicht mit der Buildkopie überein.")
    _verify_archive(archive, source, files)
    processes = _run(["/bin/ps", "-axo", "comm="]).stdout.splitlines()
    if any(
        str(Path(command.strip()).resolve()).startswith(str(source) + "/")
        for command in processes if command.strip()
    ):
        raise InstallError("Die Buildkopie wird noch ausgeführt und bleibt unverändert.")
    extension = source / EXTENSION_PATH
    inventory = _run(
        ["/usr/bin/pluginkit", "-m", "-A", "-D", "-vv", "-i", EXTENSION_ID]
    ).stdout.splitlines()
    registered_paths = {
        line.split("=", 1)[1].strip()
        for line in inventory if line.strip().startswith("Path =")
    }
    if str(extension) in registered_paths:
        # Path-specific removal of the disposable duplicate only. Never cycle
        # the permanent installation's registration or change user elections.
        _run(["/usr/bin/pluginkit", "-r", str(extension)])
    shutil.rmtree(source)


def main():
    cleanup_only = sys.argv[1:2] == ["--cleanup-only"]
    arguments = sys.argv[2:] if cleanup_only else sys.argv[1:]
    if len(arguments) not in ((3,) if cleanup_only else (2, 3)):
        raise SystemExit(
            "usage: install_safari_app.py [--cleanup-only] SOURCE.app DESTINATION.app [ARCHIVE.zip]"
        )
    try:
        if not cleanup_only:
            install(arguments[0], arguments[1])
        if len(arguments) == 3:
            cleanup_generated_bundle(*arguments)
    except (InstallError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Safari-Installation abgebrochen: {exc}") from None


if __name__ == "__main__":
    main()
