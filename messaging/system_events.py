"""Keep platform events distinct from authored messages and private API data."""

from datetime import datetime, timezone
import re

from .contracts import MessagingError, validate_id


def validate_system_events(value):
    def invalid():
        raise MessagingError(
            "system_events_invalid",
            "Einige Plattformereignisse konnten nicht eindeutig archiviert werden.",
            409,
        )

    if not isinstance(value, list) or len(value) > 200:
        invalid()
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            invalid()
        try:
            event_id = validate_id(item.get("event_id"))
        except MessagingError:
            invalid()
        event_type = item.get("event_type")
        if (
            event_id in seen
            or not isinstance(event_type, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", event_type)
        ):
            invalid()
        seen.add(event_id)
        event = {"event_id": event_id, "event_type": event_type}
        timestamp = item.get("created_at")
        if timestamp is not None:
            if not isinstance(timestamp, str) or len(timestamp) > 40:
                invalid()
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    invalid()
                event["created_at"] = parsed.astimezone(timezone.utc).isoformat()
            except (ValueError, OverflowError):
                invalid()
        if "text" in item:
            content = item["text"]
            if (
                not isinstance(content, str) or len(content) > 20_000
                or any(ord(char) < 32 and char not in "\n\r\t" for char in content)
            ):
                invalid()
            event["text"] = content
        result.append(event)
    return result
