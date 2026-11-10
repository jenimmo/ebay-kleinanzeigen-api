"""Icon appearance assets and narrow updates; never installs or signs a real app."""

import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

from PIL import Image
import pytest

import test_safari_web_update as web_tests


update = web_tests.update
update_fixture = web_tests.update_fixture
web_files = web_tests.web_files


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))
try:
    import safari_icon_assets as icons
    spec = importlib.util.spec_from_file_location("safari_icon_update", ROOT / "script/update_safari_icons.py")
    icon_update = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(icon_update)
finally:
    sys.path.pop(0)


def test_original_icon_assets_include_all_native_sizes_and_true_monochrome_toolbar():
    assert len(icons.web_icon_files()) == 7
    assert icons.app_icon().startswith(b"icns")
    for points in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            suffix = "@2x" if scale == 2 else ""
            with Image.open(icons.ASSETS / "KleinanzeigenSafari.iconset" / f"icon_{points}x{points}{suffix}.png") as image:
                assert image.size == (points * scale,) * 2
                assert image.mode == "RGBA"
                assert image.getpixel((0, 0))[3] == 0
                assert image.getpixel((points * scale // 2, points * scale // 2))[3] == 255
    for size in (16, 32):
        with Image.open(icons.ASSETS / "web-icons" / f"toolbar-{size}.png") as image:
            pixels = list(image.get_flattened_data())
            assert any(pixel[3] == 0 for pixel in pixels)
            assert any(pixel[3] == 255 for pixel in pixels)
            assert all(pixel[:3] == (0, 0, 0) for pixel in pixels if pixel[3])


def test_manifest_and_xcode_resource_mapping_are_complete():
    manifest = json.loads((ROOT / "safari-extension/web/manifest.json").read_text())
    assert icons.manifest_has_icons(manifest)
    project_dir = ROOT / "safari-extension/native/Kleinanzeigen Safari"
    project = json.loads(subprocess.run([
        "/usr/bin/plutil", "-convert", "json", "-o", "-",
        str(project_dir / "Kleinanzeigen Safari.xcodeproj/project.pbxproj"),
    ], check=True, capture_output=True).stdout)
    objects = project["objects"]
    targets = {item["name"]: item for item in objects.values() if item.get("isa") == "PBXNativeTarget"}
    resources = {}
    for name, target in targets.items():
        files = []
        for phase_id in target["buildPhases"]:
            phase = objects[phase_id]
            if phase["isa"] == "PBXResourcesBuildPhase":
                for build_id in phase["files"]:
                    resource = objects[objects[build_id]["fileRef"]]
                    path = (project_dir / resource["path"]).resolve()
                    assert path.is_file()
                    files.append(path.name)
        assert len(files) == len(set(files))
        resources[name] = set(files)
    assert icons.WEB_ICON_FILES <= resources["Kleinanzeigen Safari Extension"]
    assert update.WEB_FILES <= resources["Kleinanzeigen Safari Extension"]
    assert resources["Kleinanzeigen Safari"] == {icons.APP_ICON_NAME + ".icns"}
    info = plistlib.loads((project_dir / "Kleinanzeigen Safari/Info.plist").read_bytes())
    assert info["CFBundleIconFile"] == icons.APP_ICON_NAME


@pytest.fixture
def icon_fixture(update_fixture, monkeypatch):
    app, archive, state, stage = update_fixture
    monkeypatch.setattr(icon_update, "web", update)
    monkeypatch.setattr(icon_update, "installer", update.installer)
    extension_resources = app / update.installer.EXTENSION_PATH / "Contents/Resources"
    source = json.loads((update.WEB / "manifest.json").read_text())
    source["browser_action"] = {"default_popup": "popup.html", "default_title": "Nachrichten"}
    previous = {**source, "version": "1.0.4"}
    (extension_resources / "manifest.json").write_text(json.dumps(previous))
    shutil.copyfile(update.WEB / "messaging-read.js", extension_resources / "messaging-read.js")
    (update.WEB / "manifest.json").write_text(json.dumps(icons.icon_manifest(previous, "1.0.5")))
    return app, archive, state, stage


def test_icon_update_preserves_all_program_and_profile_bytes(icon_fixture):
    app, archive, state, stage = icon_fixture
    before = update.installer._bundle_files(app)
    icon_update.update("1.0.5", 13)
    assert state["install_count"] == 1 and archive.is_file() and not stage.exists()
    after = update.installer._bundle_files(app)
    for name, digest in before.items():
        if name.endswith((".js", ".html", ".css", ".dylib", ".provisionprofile")):
            assert after[name] == digest
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert info["CFBundleIconFile"] == icons.APP_ICON_NAME
    assert (app / icons.APP_ICON_PATH).read_bytes() == icons.app_icon()
    assert state["signs"][0][-1].endswith(".appex")
    assert state["signs"][1][-1].endswith(".app")
    assert all("--deep" not in command for command in state["signs"])


@pytest.mark.parametrize("mutation", ["javascript", "permissions", "host_icon", "corrupt", "rights"])
def test_icon_update_rejects_unrelated_mutations_before_install(icon_fixture, mutation):
    app, archive, state, stage = icon_fixture
    if mutation in {"corrupt", "rights"}:
        state[mutation] = True
    elif mutation == "javascript":
        (update.WEB / "content.js").write_text("unreviewed code change")
    elif mutation == "permissions":
        manifest = json.loads((update.WEB / "manifest.json").read_text())
        manifest["permissions"].append("<all_urls>")
        (update.WEB / "manifest.json").write_text(json.dumps(manifest))
    else:
        path = app / "Contents/Info.plist"
        info = plistlib.loads(path.read_bytes())
        info["CFBundleIconFile"] = "Unrelated"
        path.write_bytes(plistlib.dumps(info))
    before = update.installer._bundle_files(app)
    with pytest.raises(update.installer.InstallError):
        icon_update.update("1.0.5", 13)
    assert update.installer._bundle_files(app) == before
    assert state["install_count"] == 0 and not archive.exists() and not stage.exists()


def test_followup_web_update_keeps_icon_contract_and_cannot_introduce_it(web_files):
    web, old = web_files
    old["browser_action"] = {"default_popup": "popup.html"}
    current = json.loads((web / "manifest.json").read_text())
    current["browser_action"] = old["browser_action"].copy()
    with_icons = icons.icon_manifest(current, "1.0.5")
    (web / "manifest.json").write_text(json.dumps(with_icons))
    with pytest.raises(update.installer.InstallError, match="Manifestrechte"):
        update.validate_web_files(web, old, "1.0.5")
    result = update.validate_web_files(web, with_icons, "1.0.5")
    assert set(result) == update.WEB_FILES | icons.WEB_ICON_FILES


@pytest.mark.parametrize("mutation", ["size", "crc", "symlink", "extra"])
def test_icon_asset_guards_reject_invalid_inputs(tmp_path, monkeypatch, mutation):
    root = tmp_path / "assets"
    shutil.copytree(icons.ASSETS / "web-icons", root / "web-icons")
    monkeypatch.setattr(icons, "ASSETS", root)
    path = root / "web-icons/toolbar-16.png"
    if mutation == "size":
        path.write_bytes((root / "web-icons/toolbar-32.png").read_bytes())
    elif mutation == "crc":
        data = bytearray(path.read_bytes())
        data[50] ^= 1
        path.write_bytes(data)
    elif mutation == "symlink":
        path.unlink()
        path.symlink_to(root / "web-icons/toolbar-32.png")
    else:
        (root / "web-icons/extra.js").write_text("not an icon")
    with pytest.raises(update.installer.InstallError):
        icons.web_icon_files()
