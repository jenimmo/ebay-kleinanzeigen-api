"""Small ASGI security guards shared by all HTTP endpoints."""

from collections.abc import Awaitable, Callable
from ipaddress import ip_address
from typing import Any

from starlette.responses import JSONResponse, PlainTextResponse


ASGIMessage = dict[str, Any]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]


class _RequestBodyTooLarge(BaseException):
    """Internal control-flow exception for streamed request bodies."""


class RequestBodyLimitMiddleware:
    """Reject request bodies before they can grow without a fixed bound."""

    def __init__(self, app: Any, max_body_size: int = 64 * 1024) -> None:
        if max_body_size < 1:
            raise ValueError("max_body_size must be at least 1")

        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = self._content_length(scope.get("headers", []))
        if content_length is not None and content_length > self.max_body_size:
            await self._reject(scope, receive, send)
            return

        bytes_received = 0
        response_started = False

        async def limited_receive() -> ASGIMessage:
            nonlocal bytes_received
            message = await receive()
            if message["type"] == "http.request":
                bytes_received += len(message.get("body", b""))
                if bytes_received > self.max_body_size:
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
