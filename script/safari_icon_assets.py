"""Exact local icon contract shared by resource-only Safari update tools."""

from __future__ import annotations

import binascii
import copy
from pathlib import Path
import plistlib
import struct

import install_safari_app as installer


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "safari-extension/assets"
APP_ICON_NAME = "KleinanzeigenSafari"
APP_ICON_PATH = "Contents/Resources/" + APP_ICON_NAME + ".icns"
COLOR_ICONS = {str(size): f"icon-{size}.png" for size in (16, 32, 48, 128, 256)}
TOOLBAR_ICONS = {str(size): f"toolbar-{size}.png" for size in (16, 32)}
WEB_ICON_FILES = frozenset([*COLOR_ICONS.values(), *TOOLBAR_ICONS.values()])


def manifest_has_icons(manifest):
    action = manifest.get("browser_action")
    present = "icons" in manifest or (isinstance(action, dict) and "default_icon" in action)
    if present and not (
        manifest.get("icons") == COLOR_ICONS and isinstance(action, dict)
        and action.get("default_icon") == TOOLBAR_ICONS
    ):
        raise installer.InstallError("Das Manifest enthält nicht die festgelegten lokalen Icons.")
    return present


def icon_manifest(previous, version):
    """The only manifest changes this resource update may introduce."""
    result = copy.deepcopy(previous)
    if not isinstance(result.get("browser_action"), dict):
        raise installer.InstallError("Die erwartete Browseraktion fehlt.")
    manifest_has_icons(result)
    result.update(version=version, icons=COLOR_ICONS.copy())
    result["browser_action"]["default_icon"] = TOOLBAR_ICONS.copy()
    return result


def validate_png(data, size):
    if not isinstance(data, bytes) or not 40 <= len(data) <= 2_000_000 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise installer.InstallError("Eine Icondatei ist kein begrenztes PNG.")
    position, kinds = 8, []
    while position + 12 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, position)
        end = position + length + 12
        if end > len(data) or binascii.crc32(data[position + 4:end - 4]) & 0xFFFFFFFF != struct.unpack_from(">I", data, end - 4)[0]:
            raise installer.InstallError("Eine Icondatei enthält ungültige PNG-Daten.")
        kinds.append(kind)
        if kind == b"IHDR":
            if position != 8 or length != 13:
                raise installer.InstallError("Die PNG-Kopfzeile ist ungültig.")
            width, height, depth, color, compression, filtering, interlace = struct.unpack_from(">IIBBBBB", data, position + 8)
            if (width, height, depth, color, compression, filtering, interlace) != (size, size, 8, 6, 0, 0, 0):
                raise installer.InstallError("Die Icongröße oder das RGBA-Format stimmt nicht.")
        position = end
        if kind == b"IEND":
            if length != 0:
                raise installer.InstallError("Der PNG-Abschluss ist ungültig.")
            break
    if position != len(data) or kinds[:1] != [b"IHDR"] or kinds[-1:] != [b"IEND"] or b"IDAT" not in kinds:
        raise installer.InstallError("Eine Icondatei ist unvollständig.")
    # The visual monochrome/alpha property is verified by asset tests, not by
    # executing an image decoder while replacing a signed application bundle.
    return data


def web_icon_files():
    directory = ASSETS / "web-icons"
    if directory.is_symlink() or {path.name for path in directory.iterdir()} != WEB_ICON_FILES:
        raise installer.InstallError("Die lokalen Webicons weichen von der erlaubten Liste ab.")
    result = {}
    for name in sorted(WEB_ICON_FILES):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise installer.InstallError("Icondateien dürfen keine symbolischen Links sein.")
        size = int(name.rsplit("-", 1)[1].split(".", 1)[0])
        result[name] = validate_png(path.read_bytes(), size)
    return result


def app_icon():
    path = ASSETS / (APP_ICON_NAME + ".icns")
    if path.is_symlink() or not path.is_file():
        raise installer.InstallError("Die lokale App-Icondatei fehlt oder ist ein symbolischer Link.")
    data = path.read_bytes()
    if not 16 <= len(data) <= 2_000_000 or data[:4] != b"icns" or struct.unpack_from(">I", data, 4)[0] != len(data):
        raise installer.InstallError("Die App-Icondatei ist kein begrenztes ICNS.")
    expected = {b"ic04", b"ic05", b"ic07", b"ic08", b"ic09", b"ic10", b"ic11", b"ic12", b"ic13", b"ic14", b"info"}
    sizes = {b"ic07":128, b"ic08":256, b"ic09":512, b"ic10":1024, b"ic11":32, b"ic12":64, b"ic13":256, b"ic14":512}
    position, seen = 8, set()
    while position + 8 <= len(data):
        kind, length = struct.unpack_from(">4sI", data, position)
        if kind not in expected or kind in seen or length < 8 or position + length > len(data):
            raise installer.InstallError("Die ICNS-Größen oder Einträge sind ungültig.")
        seen.add(kind)
        contents = data[position + 8:position + length]
        if kind in sizes:
            validate_png(contents, sizes[kind])
        elif kind in {b"ic04", b"ic05"}:
            if not contents.startswith(b"ARGB") or len(contents) > 8192:
                raise installer.InstallError("Die kleinen ICNS-Varianten sind ungültig.")
        elif len(contents) > 8192 or not isinstance(plistlib.loads(contents), dict):
            raise installer.InstallError("Die ICNS-Metadaten sind ungültig.")
        position += length
    if seen != expected or position != len(data):
        raise installer.InstallError("Die App-Icondatei enthält nicht alle vorgesehenen Größen.")
    return data
