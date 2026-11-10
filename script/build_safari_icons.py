"""Rebuild the project's original vector icons; no signing or installation."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "safari-extension/assets"
ICONSET = ASSETS / "KleinanzeigenSafari.iconset"
WEB_ICONS = ASSETS / "web-icons"
COLOR_SIZES = (16, 32, 48, 128, 256)
TOOLBAR_SIZES = (16, 32)


def render(source, target, size, renderer):
    subprocess.run([
        renderer, "--width", str(size), "--height", str(size),
        "--output", str(target), str(source),
    ], check=True)


def build():
    renderer = shutil.which("rsvg-convert")
    if renderer is None:
        raise RuntimeError("Für reproduzierbare SVG-Rasterisierung wird rsvg-convert benötigt.")
    ICONSET.mkdir(parents=True, exist_ok=True)
    WEB_ICONS.mkdir(parents=True, exist_ok=True)
    for points in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            suffix = "@2x" if scale == 2 else ""
            render(ASSETS / "AppIcon.svg", ICONSET / f"icon_{points}x{points}{suffix}.png", points * scale, renderer)
    for size in COLOR_SIZES:
        render(ASSETS / "AppIcon.svg", WEB_ICONS / f"icon-{size}.png", size, renderer)
    for size in TOOLBAR_SIZES:
        render(ASSETS / "ToolbarIcon.svg", WEB_ICONS / f"toolbar-{size}.png", size, renderer)
    subprocess.run([
        "/usr/bin/iconutil", "--convert", "icns", "--output",
        str(ASSETS / "KleinanzeigenSafari.icns"), str(ICONSET),
    ], check=True)


if __name__ == "__main__":
    build()
