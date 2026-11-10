"""Bounded structural diagnostics; no message, image, URL or credential values."""


def image_structure(value):
    """Copy only authored image-guard names, enums, booleans and capped counts."""
    if not isinstance(value, dict):
        return None
    stage = value.get("stage")
    if not isinstance(stage, str) or stage not in {
        "fresh_document",
        "attachment_descriptor",
        "message_binding",
        "container",
        "image_node",
        "image_ready",
        "source_encoding",
        "mime_match",
        "base64",
        "magic_bytes",
        "preview_digest",
        "archive_transfer",
        "receipt_metadata",
        "pixel_decode",
        "pixel_compare",
        "preview_persistence",
        "viewer_binding",
        "viewer_ready",
    }:
        return None
    result = {"stage": stage}
    for name in (
        "fresh_document",
        "descriptor_present",
        "remote_id_matches",
        "sender_matches",
        "direction_matches",
        "visible",
        "complete",
        "dimensions_valid",
        "within_pixel_limit",
        "preview_digest_matches",
        "view_verified",
        "readiness_timeout",
        "pixel_space_supported",
        "pixel_format_supported",
        "pixel_dimensions_match",
        "pixel_digest_matches",
    ):
        if type(value.get(name)) is bool:
            result[name] = value[name]
    for name in (
        "attachment_count",
        "message_count",
        "container_count",
        "image_count",
    ):
        # Two means two or more; never retain counts from arbitrary payloads.
        if type(value.get(name)) is int and 0 <= value[name] <= 2:
            result[name] = value[name]
    enums = {
        "viewer_guard": {
            "existing_modal", "opener_control", "preopen_binding", "message_changed",
            "modal_changed", "modal_ownership", "modal_structure", "thumbnail_count",
            "thumbnail_visibility", "thumbnail_order", "active_thumbnail", "nested_images",
            "image_containment", "source_match", "post_identity_binding", "thumbnails_changed",
            "images_changed", "close_control",
        },
        "source_kind": {"none", "data", "blob", "https", "other"},
        "data_mime": {"jpeg", "png", "gif", "webp", "other", "missing"},
        "expected_mime": {"jpeg", "png", "gif", "webp", "other", "missing"},
        "magic_type": {"jpeg", "png", "gif", "webp", "unknown"},
    }
    for name, allowed in enums.items():
        if isinstance(value.get(name), str) and value[name] in allowed:
            result[name] = value[name]
    return result
