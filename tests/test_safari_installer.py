"""Filesystem/identity guards; no real app installation or signing-key access."""

import importlib.util
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import zipfile

import pytest


spec = importlib.util.spec_from_file_location(
    "safari_installer", Path(__file__).resolve().parents[1] / "script/install_safari_app.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def bundle(path, value, identifier=installer.APP_ID):
    (path / "Contents").mkdir(parents=True)
    (path / "Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": identifier})
    )
    extension = path / installer.EXTENSION_PATH / "Contents"
    extension.mkdir(parents=True)
    (extension / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": installer.EXTENSION_ID})
    )
    (path / "Contents/version.txt").write_text(value)
    return path


@pytest.fixture
def signed_commands(monkeypatch):
    calls = []

    def run(arguments):
        calls.append(arguments)
        if arguments[0] == "/usr/bin/ditto":
            shutil.copytree(arguments[-2], arguments[-1])
        elif arguments[0] == "/usr/bin/codesign":
            pass
        else:
            raise AssertionError("Installer must not alter registration or processes")
        return subprocess.CompletedProcess(
            arguments, 0, "", f"TeamIdentifier={installer.TEAM_ID}\n"
        )

    monkeypatch.setattr(installer, "_run", run)
    return calls


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin atomic rename API")
def test_atomic_swap_keeps_old_open_file_intact(tmp_path, signed_commands):
    source = bundle(tmp_path / "build/Source.app", "new")
    target = bundle(tmp_path / "Applications/Kleinanzeigen Safari.app", "old")
    previous_inode = target.stat().st_ino
    with (target / "Contents/version.txt").open() as old_file:
        installer.install(source, target)
        assert old_file.read() == "old"
    assert (target / "Contents/version.txt").read_text() == "new"
    assert target.stat().st_ino != previous_inode
    assert not list(target.parent.glob(".kleinanzeigen-safari-update-*"))
    assert all("pluginkit" not in argument for call in signed_commands for argument in call)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin atomic rename API")
def test_new_install_uses_complete_staged_bundle(tmp_path, signed_commands):
    source = bundle(tmp_path / "build/Source.app", "new")
    target = tmp_path / "Applications/Kleinanzeigen Safari.app"
    installer.install(source, target)
    assert (target / "Contents/version.txt").read_text() == "new"


def test_foreign_bundle_is_rejected_before_copy(tmp_path, signed_commands):
    source = bundle(tmp_path / "build/Source.app", "new")
    target = bundle(tmp_path / "Applications/Kleinanzeigen Safari.app", "unrelated", "example.other")
    with pytest.raises(installer.InstallError, match="fremde App"):
        installer.install(source, target)
    assert (target / "Contents/version.txt").read_text() == "unrelated"
    assert not any(call[0] == "/usr/bin/ditto" for call in signed_commands)


def test_symlink_destination_does_not_touch_target(tmp_path, signed_commands):
    source = bundle(tmp_path / "Source.app", "new")
    real = bundle(tmp_path / "Real.app", "old")
    target = tmp_path / "Linked.app"
    target.symlink_to(real, target_is_directory=True)
    with pytest.raises(installer.InstallError):
        installer.install(source, target)
    assert (real / "Contents/version.txt").read_text() == "old"
    assert not signed_commands


def test_wrong_signing_team_is_rejected(tmp_path, monkeypatch):
    source = bundle(tmp_path / "Source.app", "new")
    monkeypatch.setattr(
        installer,
        "_run",
        lambda args: subprocess.CompletedProcess(args, 0, "", "TeamIdentifier=OTHER\n"),
    )
    with pytest.raises(installer.InstallError, match="Projektteam"):
        installer.install(source, tmp_path / "Target.app")
    assert not (tmp_path / "Target.app").exists()


def test_staged_signature_failure_preserves_installed_app(tmp_path, signed_commands, monkeypatch):
    source = bundle(tmp_path / "Source.app", "new")
    target = bundle(tmp_path / "Target.app", "old")
    verify = installer.verify_bundle

    def reject_stage(path):
        if path.parent.name.startswith(".kleinanzeigen-safari-update-"):
            raise installer.InstallError("staged signature rejected")
        verify(path)

    monkeypatch.setattr(installer, "verify_bundle", reject_stage)
    with pytest.raises(installer.InstallError, match="staged signature"):
        installer.install(source, target)
    assert (target / "Contents/version.txt").read_text() == "old"


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin atomic rename API")
def test_failed_final_verification_restores_complete_old_bundle(tmp_path, signed_commands, monkeypatch):
    source = bundle(tmp_path / "Source.app", "new")
    target = bundle(tmp_path / "Target.app", "old")
    verify = installer.verify_bundle

    def reject_installed_new(path):
        if path == target and (path / "Contents/version.txt").read_text() == "new":
            raise installer.InstallError("post-install check rejected")
        verify(path)

    monkeypatch.setattr(installer, "verify_bundle", reject_installed_new)
    with pytest.raises(installer.InstallError, match="post-install"):
        installer.install(source, target)
    assert (target / "Contents/version.txt").read_text() == "old"


def test_atomic_rename_failure_has_no_in_place_fallback(tmp_path, signed_commands, monkeypatch):
    source = bundle(tmp_path / "Source.app", "new")
    target = bundle(tmp_path / "Target.app", "old")

    def fail(*args, **kwargs):
        raise OSError("atomic rename unavailable")

    monkeypatch.setattr(installer, "_rename", fail)
    with pytest.raises(OSError, match="atomic rename"):
        installer.install(source, target)
    assert (target / "Contents/version.txt").read_text() == "old"


@pytest.fixture
def cache_cleanup(tmp_path, signed_commands, monkeypatch):
    source = bundle(tmp_path / "cache/Kleinanzeigen Safari.app", "installed")
    target = bundle(tmp_path / "Applications/Kleinanzeigen Safari.app", "installed")
    archive = tmp_path / "Kleinanzeigen Safari.zip"
    with zipfile.ZipFile(archive, "w") as saved:
        for entry in source.rglob("*"):
            if entry.is_file():
                saved.write(entry, source.name + "/" + entry.relative_to(source).as_posix())
    monkeypatch.setattr(installer, "_generated_bundle_path", lambda: source)
    monkeypatch.setattr(installer, "_installed_bundle_path", lambda: target)
    original_run = installer._run
    state = {"processes": "", "registered": True, "calls": []}

    def run(arguments):
        state["calls"].append(arguments)
        if arguments[0] == "/bin/ps":
            return subprocess.CompletedProcess(arguments, 0, state["processes"], "")
        if arguments[0] == "/usr/bin/pluginkit":
            if arguments[1] == "-r":
                assert arguments[2] == str(source / installer.EXTENSION_PATH)
                state["registered"] = False
                return subprocess.CompletedProcess(arguments, 0, "", "")
            paths = [target / installer.EXTENSION_PATH]
            if state["registered"]:
                paths.append(source / installer.EXTENSION_PATH)
            output = "\n".join("    Path = " + str(path) for path in paths)
            return subprocess.CompletedProcess(arguments, 0, output, "")
        return original_run(arguments)

    monkeypatch.setattr(installer, "_run", run)
    return source, target, archive, state


def test_cleanup_only_unregisters_archived_identical_generated_copy(cache_cleanup):
    source, target, archive, state = cache_cleanup
    installed_inode = target.stat().st_ino
    installer.cleanup_generated_bundle(source, target, archive)
    assert not source.exists()
    assert target.stat().st_ino == installed_inode and archive.is_file()
    assert [call for call in state["calls"] if "-r" in call] == [
        ["/usr/bin/pluginkit", "-r", str(source / installer.EXTENSION_PATH)]
    ]
    assert not any("-a" in call or "-e" in call for call in state["calls"])


def test_cleanup_without_duplicate_does_not_change_registration(cache_cleanup):
    source, target, archive, state = cache_cleanup
    state["registered"] = False
    installer.cleanup_generated_bundle(source, target, archive)
    assert not source.exists() and target.is_dir()
    assert not any("-r" in call for call in state["calls"])


def test_cleanup_refuses_unexpected_path(cache_cleanup, tmp_path):
    source, target, archive, state = cache_cleanup
    other = bundle(tmp_path / "Another.app", "installed")
    with pytest.raises(installer.InstallError, match="festen Projektpfade"):
        installer.cleanup_generated_bundle(other, target, archive)
    assert source.is_dir() and other.is_dir() and not state["calls"]


def test_cleanup_refuses_different_installed_contents(cache_cleanup):
    source, target, archive, state = cache_cleanup
    (target / "Contents/version.txt").write_text("older")
    with pytest.raises(installer.InstallError, match="Buildkopie"):
        installer.cleanup_generated_bundle(source, target, archive)
    assert source.is_dir() and state["registered"]


def test_cleanup_refuses_invalid_archive(cache_cleanup):
    source, target, archive, state = cache_cleanup
    archive.write_bytes(b"invalid zip")
    with pytest.raises(installer.InstallError, match="Sicherungsarchiv"):
        installer.cleanup_generated_bundle(source, target, archive)
    assert source.is_dir() and state["registered"]


def test_cleanup_refuses_running_cache_binary_without_killing_it(cache_cleanup):
    source, target, archive, state = cache_cleanup
    state["processes"] = str(source / installer.EXTENSION_PATH / "Contents/MacOS/Extension")
    with pytest.raises(installer.InstallError, match="ausgeführt"):
        installer.cleanup_generated_bundle(source, target, archive)
    assert source.is_dir() and state["registered"]
    assert not any(call[0] == "/usr/bin/pluginkit" for call in state["calls"])


def test_cleanup_checks_generated_identity_before_removing_it(cache_cleanup):
    source, target, archive, state = cache_cleanup
    (source / "Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "example.unrelated"})
    )
    with pytest.raises(installer.InstallError, match="fremde App"):
        installer.cleanup_generated_bundle(source, target, archive)
    assert source.is_dir() and not state["calls"]
