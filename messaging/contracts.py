"""Shared limits and sanitised errors for the messaging subsystem."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_ATTACHMENTS = 20
MAX_TEXT_LENGTH = 20_000
ACCOUNT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"


class MessagingError(Exception):
    """Only deliberately authored messages may cross the public boundary."""

    def __init__(self, code: str, message: str, status_code: int = 400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, **self.details}


def validate_id(value: str, *, account: bool = False) -> str:
    pattern = ACCOUNT_ID_PATTERN if account else OPAQUE_ID_PATTERN
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise MessagingError("invalid_id", "Ungültige Account- oder Objekt-ID.", 422)
    return value


@dataclass(frozen=True)
class MessagingSettings:
    root: Path = Path("data/messaging")
    browser_channel: str = "chrome"
    safari_endpoint: str = "http://127.0.0.1:8000/messaging/safari-bridge/exchange"
    test_mode: bool = False
    test_account_ids: frozenset[str] = frozenset()
    test_user_ids: frozenset[str] = frozenset()
    test_listing_ids: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls):
        prefix = "KLEINANZEIGEN_MESSAGING_"
        raw = os.environ.get(prefix + "TEST_MODE", "false").lower()
        if raw not in {"true", "false", "1", "0"}:
            raise ValueError(prefix + "TEST_MODE must be true or false")
        browser_channel = os.environ.get(prefix + "BROWSER_CHANNEL", "chrome")
        if browser_channel not in {"chrome", "chromium", "safari"}:
            raise ValueError(
                prefix + "BROWSER_CHANNEL must be chrome, chromium or safari"
            )
        safari_endpoint = os.environ.get(
            prefix + "SAFARI_ENDPOINT",
            "http://127.0.0.1:8000/messaging/safari-bridge/exchange",
        )
        validate_safari_endpoint(safari_endpoint)

        def ids(name, account=False):
            values = frozenset(
                x.strip()
                for x in os.environ.get(prefix + name, "").split(",")
                if x.strip()
            )
            for value in values:
                validate_id(value, account=account)
            return values

        return cls(
            browser_channel=browser_channel,
            safari_endpoint=safari_endpoint,
            root=Path(os.environ.get(prefix + "DIR", "data/messaging"))
            .expanduser()
            .absolute(),
            test_mode=raw in {"true", "1"},
            test_account_ids=ids("TEST_ACCOUNT_IDS", True),
            test_user_ids=ids("TEST_USER_IDS"),
            test_listing_ids=ids("TEST_LISTING_IDS"),
        )

    def public_policy(self):
        return {
            "enabled": self.test_mode,
            "account_ids": sorted(self.test_account_ids),
            "user_ids": sorted(self.test_user_ids),
            "listing_ids": sorted(self.test_listing_ids),
        }


def validate_safari_endpoint(value: str) -> str:
    """Native messaging may contact only this exact local bridge route."""
    try:
        parsed = urlsplit(value)
        valid = (
            isinstance(value, str)
            and not any(ord(char) < 33 or char == "\\" for char in value)
            and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "::1"}
            and parsed.username is None
            and parsed.password is None
            and parsed.port is not None
            and 1 <= parsed.port <= 65535
            and parsed.path == "/messaging/safari-bridge/exchange"
            and not parsed.query
            and not parsed.fragment
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError(
            "KLEINANZEIGEN_MESSAGING_SAFARI_ENDPOINT must be an explicit loopback bridge URL"
        )
    return value
