"""Native extension update guards; no Xcode build, signing or real installation."""

import copy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "script"
sys.path.insert(0, str(SCRIPT))
try:
    spec = importlib.util.spec_from_file_location("safari_extension_update", SCRIPT / "update_safari_extension.py")
    update = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(update)
finally:
    sys.path.pop(0)


def project():
    return {"objects": {
        "extension": {"isa": "PBXNativeTarget", "name": update.TARGET,
                      "productType": "com.apple.product-type.app-extension",
                      "dependencies": [], "buildPhases": ["sources"]},
        "sources": {"isa": "PBXSourcesBuildPhase"},
        "host": {"isa": "PBXNativeTarget", "name": "Kleinanzeigen Safari",
                 "productType": "com.apple.product-type.application"},
    }}


def test_only_dependency_free_extension_target_can_be_built():
    update.validate_target(project())


@pytest.mark.parametrize("mutation", ["host_product", "dependency", "script", "duplicate", "missing"])
def test_native_target_cannot_introduce_host_build_or_shell_phase(mutation):
    value = project()
    target = value["objects"]["extension"]
    if mutation == "host_product":
        target["productType"] = "com.apple.product-type.application"
    elif mutation == "dependency":
        target["dependencies"] = ["host"]
    elif mutation == "script":
        value["objects"]["sources"]["isa"] = "PBXShellScriptBuildPhase"
    elif mutation == "duplicate":
        value["objects"]["duplicate"] = copy.deepcopy(target)
    else:
        del value["objects"]["extension"]
    with pytest.raises(update.installer.InstallError, match="Extension-Target"):
        update.validate_target(value)


def test_build_command_uses_existing_managed_profile_without_provisioning_updates(tmp_path):
    arguments = update.build_arguments(tmp_path, "1.0.16", 24)
    assert arguments[arguments.index("-target") + 1] == update.TARGET
    assert "-scheme" not in arguments and "-alltargets" not in arguments
    assert "-allowProvisioningUpdates" not in arguments
    assert "CODE_SIGN_STYLE=Automatic" in arguments
    assert "CODE_SIGN_IDENTITY=Apple Development" in arguments
    assert not any(arg.startswith("PROVISIONING_PROFILE_SPECIFIER=") for arg in arguments)
    assert "DEVELOPMENT_TEAM=" + update.installer.TEAM_ID in arguments
    assert "SYMROOT=" + str(tmp_path / "products") in arguments


def bundle_files():
    return {
        "Contents/MacOS/Host": b"host-code",
        "Contents/Info.plist": b"version",
        "Contents/_CodeSignature/CodeResources": b"signature",
        "Contents/embedded.provisionprofile": b"existing-profile",
        "Contents/MacOS/Host.debug.dylib": b"unchanged-dylib",
        update.installer.EXTENSION_PATH + "/Contents/MacOS/Extension": b"old-extension",
    }


def test_extension_may_change_but_host_payload_scope_stays_fixed():
    before = bundle_files()
    after = {**before, "Contents/Info.plist": b"new-version", "Contents/_CodeSignature/CodeResources": b"new-signature"}
    after[update.installer.EXTENSION_PATH + "/Contents/MacOS/Extension"] = b"new-extension"
    after[update.installer.EXTENSION_PATH + "/Contents/Resources/content.js"] = b"new-web-code"
    update.validate_host_changes(before, after, "Contents/MacOS/Host")


@pytest.mark.parametrize("mutation", ["profile", "dylib", "new_host_file", "missing_host_file"])
def test_native_update_cannot_change_host_profile_or_add_unreviewed_files(mutation):
    before = bundle_files()
    after = dict(before)
    if mutation == "profile":
        after["Contents/embedded.provisionprofile"] = b"different-profile"
    elif mutation == "dylib":
        after["Contents/MacOS/Host.debug.dylib"] = b"new-code"
    elif mutation == "new_host_file":
        after["Contents/Resources/other.js"] = b"unexpected"
    else:
        del after["Contents/Info.plist"]
    with pytest.raises(update.installer.InstallError, match="Host-Dateien"):
        update.validate_host_changes(before, after, "Contents/MacOS/Host")


@pytest.mark.parametrize("active", ["registration", "process"])
def test_cleanup_preserves_unexpected_active_private_stage(tmp_path, monkeypatch, active):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "keep").write_text("private artifact")
    registered = [(str(stage / "unexpected.appex"), "1.0.16")] if active == "registration" else []
    process = str(stage / "unexpected.app/Contents/MacOS/Extension") if active == "process" else ""
    monkeypatch.setattr(update.web_update, "inventory", lambda: registered)
    monkeypatch.setattr(update.web_update, "run", lambda _: SimpleNamespace(stdout=process.encode()))
    with pytest.raises(update.installer.InstallError, match="aktive Buildkopie"):
        update.cleanup_stage(stage)
    assert (stage / "keep").read_text() == "private artifact"


def test_cleanup_removes_only_unused_private_stage(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    neighbor = tmp_path / "neighbor"
    neighbor.mkdir()
    monkeypatch.setattr(update.web_update, "inventory", lambda: [(str(neighbor / "installed.appex"), "1.0.15")])
    monkeypatch.setattr(update.web_update, "run", lambda _: SimpleNamespace(stdout=str(neighbor / "running").encode()))
    update.cleanup_stage(stage)
    assert not stage.exists()
    assert neighbor.is_dir()
