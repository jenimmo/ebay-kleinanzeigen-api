"""Small ASGI security guards shared by all HTTP endpoints."""

from collections.abc import Awaitable, Callable
from ipaddress import ip_address
from typing import Any
import re
from urllib.parse import urlsplit

from starlette.responses import JSONResponse, PlainTextResponse


ASGIMessage = dict[str, Any]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]


class _RequestBodyTooLarge(BaseException):
    """Internal control-flow exception for streamed request bodies."""


class RequestBodyLimitMiddleware:
    """Reject request bodies before they can grow without a fixed bound."""

    def __init__(
        self,
        app: Any,
        max_body_size: int = 64 * 1024,
        messaging_upload_max_size: int | None = None,
    ) -> None:
        if max_body_size < 1:
            raise ValueError("max_body_size must be at least 1")
        if messaging_upload_max_size is not None and (
            isinstance(messaging_upload_max_size, bool)
            or not isinstance(messaging_upload_max_size, int)
            or messaging_upload_max_size < 1
        ):
            raise ValueError("messaging_upload_max_size must be a positive integer")

        self.app = app
        self.max_body_size = max_body_size
        self.messaging_upload_max_size = messaging_upload_max_size

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        maximum = self.max_body_size
        if self.messaging_upload_max_size is not None and _is_messaging_image_upload(
            scope
        ):
            maximum = self.messaging_upload_max_size
        content_length = self._content_length(scope.get("headers", []))
        if content_length is not None and content_length > maximum:
            await self._reject(scope, receive, send)
            return

        bytes_received = 0
        response_started = False

        async def limited_receive() -> ASGIMessage:
            nonlocal bytes_received
            message = await receive()
            if message["type"] == "http.request":
                bytes_received += len(message.get("body", b""))
                if bytes_received > maximum:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message: ASGIMessage) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            await self._reject(scope, receive, send)

    @staticmethod
    def _content_length(headers: list[tuple[bytes, bytes]]) -> int | None:
        for name, value in headers:
            if name.lower() == b"content-length":
                try:
                    parsed = int(value)
                except ValueError:
                    return None
                return parsed if parsed >= 0 else None
        return None

    @staticmethod
    async def _reject(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": "Request body too large"},
        )
        await response(scope, receive, send)


class LoopbackOnlyMiddleware:
    """Reject network peers outside the local host for unauthenticated routes."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send):
        if scope["type"] == "http" and not self._is_loopback(scope.get("client")):
            response = PlainTextResponse(
                "MCP endpoint is available only from loopback",
                status_code=403,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _is_loopback(client: object) -> bool:
        if not isinstance(client, (tuple, list)) or not client:
            return False
        try:
            address = ip_address(str(client[0]))
        except ValueError:
            return False
        if address.is_loopback:
            return True
        mapped = getattr(address, "ipv4_mapped", None)
        return bool(mapped is not None and mapped.is_loopback)


class MessagingAccessMiddleware:
    """Private REST routes reject remote peers, DNS rebinding and browser CSRF."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path == "/messaging" or path.startswith("/messaging/")
        ):
            await self.app(scope, receive, send)
            return
        header_pairs = [
            (k.lower(), v.decode("latin1")) for k, v in scope.get("headers", [])
        ]
        headers = dict(header_pairs)
        ambiguous = any(
            sum(key == name for key, _ in header_pairs) > 1
            for name in (b"host", b"origin", b"content-type", b"sec-fetch-site")
        )
        host = headers.get(b"host", "")
        try:
            parsed_host = urlsplit("http://" + host)
            valid_host = bool(
                re.fullmatch(
                    r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?",
                    host,
                    re.IGNORECASE,
                )
            )
            if parsed_host.port is not None:
                valid_host = valid_host and 1 <= parsed_host.port <= 65535
            origin = headers.get(b"origin")
            # An absent Origin identifies a native API client. Present-but-empty
            # and opaque `null` origins do not identify native clients.
            valid_origin = origin is None or (
                scope.get("scheme") in {"http", "https"}
                and origin == f"{scope['scheme']}://{host}"
            )
        except ValueError:
            valid_host = valid_origin = False
        if (
            ambiguous
            or not LoopbackOnlyMiddleware._is_loopback(scope.get("client"))
            or not valid_host
            or not valid_origin
            or headers.get(b"sec-fetch-site", "").lower() == "cross-site"
        ):
            await JSONResponse(
                {
                    "detail": {
                        "code": "messaging_forbidden",
                        "message": "Nachrichten sind nur über einen lokalen, vertrauenswürdigen Zugriff verfügbar.",
                    }
                },
                status_code=403,
            )(scope, receive, send)
            return
        if scope.get("method") == "POST" and not _is_messaging_image_upload(scope):
            # Browser forms must not be able to trigger account or send actions.
            if (
                headers.get(b"content-type", "").split(";")[0].strip().lower()
                != "application/json"
            ):
                await JSONResponse(
                    {
                        "detail": {
                            "code": "json_required",
                            "message": "JSON erforderlich.",
                        }
                    },
                    status_code=415,
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _is_messaging_image_upload(scope: dict[str, Any]) -> bool:
    """Only one exact POST route with an image media type gets the larger limit."""
    if scope.get("method") != "POST" or not re.fullmatch(
        r"/messaging/accounts/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/attachments",
        scope.get("path", ""),
    ):
        return False
    media_types = [
        value.decode("latin1").split(";", 1)[0].strip().lower()
        for name, value in scope.get("headers", [])
        if name.lower() == b"content-type"
    ]
    return (
        len(media_types) == 1
        and re.fullmatch(r"image/[a-z0-9!#$&^_.+-]+", media_types[0]) is not None
    )
