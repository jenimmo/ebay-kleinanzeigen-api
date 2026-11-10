"""Private pairing document imported by the signed Safari host application."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets

from .contracts import validate_safari_endpoint


def export_safari_pairing(root: Path, config: dict) -> Path:
    validate_safari_endpoint(config["endpoint"])
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Safari pairing requires the private messaging directory")
    target = root / "safari-pairing.json"
    if target.is_symlink():
        raise ValueError("Safari pairing file must not be a symbolic link")
    temporary = root / (".safari-pairing-" + secrets.token_hex(12))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
