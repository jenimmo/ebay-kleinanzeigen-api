"""Exact byte/pixel image evidence, separate from permissive diagnostics."""

import re


MAX_PROOF_PIXELS = 4_000_000


def _sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def pixel_proof(value):
    """Validate the complete unscaled sRGB RGBA8 raster contract."""
    if (
        not isinstance(value, dict)
        or set(value) != {"algorithm", "width", "height", "sha256"}
        or value.get("algorithm") != "srgb-rgba8-v1"
        or type(value.get("width")) is not int
        or type(value.get("height")) is not int
        or not 1 <= value["width"] <= MAX_PROOF_PIXELS
        or not 1 <= value["height"] <= MAX_PROOF_PIXELS
        or value["width"] * value["height"] > MAX_PROOF_PIXELS
        or not _sha256(value.get("sha256"))
    ):
        raise ValueError("invalid image pixel proof")
    return dict(value)


def image_preview(value):
    """A preview acknowledgement contains hashes/shape, never image bytes."""
    if (
        not isinstance(value, dict)
        or set(value) not in ({"preview_sha256"}, {"preview_sha256", "image_pixels"})
        or not _sha256(value.get("preview_sha256"))
    ):
        raise ValueError("invalid image preview proof")
    result = {"preview_sha256": value["preview_sha256"]}
    if "image_pixels" in value:
        result["image_pixels"] = pixel_proof(value["image_pixels"])
    return result


def receipt_evidence(value, preview_sha256, preview_pixels=None):
    """Prefer byte identity; permit only an exactly matching committed raster."""
    if not _sha256(preview_sha256) or not isinstance(value, dict):
        raise ValueError("invalid image receipt proof")
    byte_evidence = {"source": "fresh_conversation_dom", "sha256": preview_sha256}
    if value == byte_evidence:
        return byte_evidence
    if (
        set(value) != {"source", "sha256", "comparison", "pixels"}
        or value.get("source") != "fresh_conversation_dom"
        or value.get("comparison") != "exact_pixels"
        or not _sha256(value.get("sha256"))
        or value["sha256"] == preview_sha256
    ):
        raise ValueError("invalid image receipt proof")
    expected = pixel_proof(preview_pixels)
    received = pixel_proof(value.get("pixels"))
    if received != expected:
        raise ValueError("image pixels differ")
    return {
        "source": "fresh_conversation_dom",
        "sha256": value["sha256"],
        "comparison": "exact_pixels",
        "pixels": received,
    }
