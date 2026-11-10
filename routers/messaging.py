"""Private account-scoped messaging API; no browser or filesystem paths in HTTP input."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from messaging.contracts import (
    ACCOUNT_ID_PATTERN,
    MAX_ATTACHMENTS,
    MAX_IMAGE_BYTES,
    MAX_TEXT_LENGTH,
    OPAQUE_ID_PATTERN,
    MessagingError,
)
from messaging.safari import _reader_structure
from messaging.diagnostics import image_structure
from utils.input_validation import normalize_listing_id


class PrivateMessagingRoute(APIRoute):
    """Do not echo message text, local filenames, or login inputs in 422 errors."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "invalid_argument",
                        "message": "Die Nachrichtenargumente sind ungültig.",
                    },
                ) from None

        return private_handler


router = APIRouter(
    prefix="/messaging", tags=["messaging"], route_class=PrivateMessagingRoute
)
ACCOUNT_PATTERN = ACCOUNT_ID_PATTERN
OBJECT_PATTERN = OPAQUE_ID_PATTERN
KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
AccountPath = Annotated[str, Path(pattern=ACCOUNT_PATTERN)]
ObjectPath = Annotated[str, Path(pattern=OBJECT_PATTERN)]
AttachmentId = Annotated[str, Field(strict=True, pattern=OBJECT_PATTERN)]


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    label: str | None = Field(default=None, min_length=1, max_length=120)


class SendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    attachment_ids: list[AttachmentId] = Field(
        default_factory=list, max_length=MAX_ATTACHMENTS
    )
    idempotency_key: str = Field(pattern=KEY_PATTERN)

    @model_validator(mode="after")
    def require_content(self) -> SendRequest:
        if not (self.text and self.text.strip()) and not self.attachment_ids:
            raise ValueError("A message requires text or images")
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("attachment_ids must be unique")
        return self


class StartConversationRequest(SendRequest):
    listing_id: str = Field(min_length=1, max_length=128)

    @field_validator("listing_id")
    @classmethod
    def validate_listing_id(cls, value: str) -> str:
        return normalize_listing_id(value)


class ReplyRequest(SendRequest):
    conversation_id: str = Field(pattern=OBJECT_PATTERN)


def _service(request: Request) -> Any:
    service = getattr(request.app.state, "messaging_service", None)
    if service is None:
        raise HTTPException(
            503,
            detail={
                "code": "messaging_unavailable",
                "message": "Messaging service is unavailable",
            },
        )
    return service


async def _call(awaitable: Any) -> Any:
    try:
        return await awaitable
    except MessagingError as exc:
        # Only the service's public error contract crosses the HTTP boundary.
        detail = {"code": exc.code, "message": exc.message}
        diagnostic = _reader_structure(
            exc.details.get("reader_structure")
            if isinstance(exc.details, dict)
            else None
        )
        if diagnostic is not None:
            detail["reader_structure"] = diagnostic
        image_diagnostic = image_structure(
            exc.details.get("image_structure")
            if isinstance(exc.details, dict)
            else None
        )
        if image_diagnostic is not None:
            detail["image_structure"] = image_diagnostic
        raise HTTPException(
            status_code=exc.status_code,
            detail=detail,
        ) from None


@router.get("/safari-bridge/status", include_in_schema=False)
async def safari_bridge_status(request: Request, response: Response) -> dict[str, Any]:
    """Read-only local diagnostics under the existing private-route guard."""
    response.headers["Cache-Control"] = "no-store"
    bridge = getattr(request.app.state, "safari_bridge", None)
    if bridge is None:
        return {"enabled": False}
    return await _call(bridge.status())


@router.post("/safari-bridge/exchange", include_in_schema=False)
async def safari_bridge_exchange(
    request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    """Native Safari host only; existing public/private API limits stay intact."""
    bridge = getattr(request.app.state, "safari_bridge", None)
    authorization = request.headers.getlist("authorization")
    if (
        bridge is None
        or len(authorization) != 1
        or not authorization[0].startswith("Bearer ")
        or "origin" in request.headers
        or "sec-fetch-site" in request.headers
        or not bridge.authenticate(authorization[0][7:])
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "safari_bridge_forbidden",
                "message": "Safari-Brücke nicht freigegeben.",
            },
        )
    native_preview = request.headers.getlist("x-kleinanzeigen-native-image-preview")
    result = await _call(bridge.exchange(
        body, native_image_preview_capable=native_preview == ["1"]
    ))
    return result


@router.get("/accounts")
async def list_accounts(request: Request) -> dict[str, Any]:
    return await _call(_service(request).list_accounts())


@router.get("/accounts/{account_id}/safari-ui", include_in_schema=False)
async def inspect_safari_ui(
    request: Request,
    response: Response,
    account_id: AccountPath,
    view: Literal["current", "inbox", "listing"] = "current",
    listing_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> dict[str, Any]:
    """Fixed structural metadata only; opening a live inbox may mark it read."""
    response.headers["Cache-Control"] = "no-store"
    return await _call(
        _service(request).inspect_safari_ui(
            account_id, view=view, listing_id=listing_id
        )
    )


@router.post("/accounts/{account_id}/connect", status_code=202)
async def connect_account(
    request: Request, account_id: AccountPath, body: ConnectRequest
) -> dict[str, Any]:
    return await _call(_service(request).connect_account(account_id, label=body.label))


@router.post("/accounts/{account_id}/disconnect")
async def disconnect_account(
    request: Request, account_id: AccountPath
) -> dict[str, Any]:
    return await _call(_service(request).disconnect_account(account_id))


@router.get("/accounts/{account_id}/conversations")
async def list_conversations(
    request: Request,
    account_id: AccountPath,
    cursor: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    unread_only: bool = False,
    source: Literal["live", "archive"] = "live",
) -> dict[str, Any]:
    return await _call(
        _service(request).list_conversations(
            account_id,
            cursor=cursor,
            limit=limit,
            unread_only=unread_only,
            source=source,
        )
    )


@router.get("/accounts/{account_id}/conversations/{conversation_id}")
async def get_conversation(
    request: Request,
    account_id: AccountPath,
    conversation_id: ObjectPath,
    cursor: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    source: Literal["live", "archive"] = "live",
) -> dict[str, Any]:
    return await _call(
        _service(request).get_conversation(
            account_id, conversation_id, cursor=cursor, limit=limit, source=source
        )
    )


@router.get("/accounts/{account_id}/images/{image_id}")
async def get_message_image(
    request: Request, account_id: AccountPath, image_id: ObjectPath
) -> Response:
    result = await _call(_service(request).get_message_image(account_id, image_id))
    return Response(
        content=result["data"],
        media_type=result["mime_type"],
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/accounts/{account_id}/attachments", status_code=201)
async def upload_image(request: Request, account_id: AccountPath) -> dict[str, Any]:
    # The global middleware applies a separate bounded allowance on this route.
    # Bound the stream here as well, so standalone router users remain protected.
    if (
        not request.headers.get("content-type", "")
        .split(";", 1)[0]
        .lower()
        .startswith("image/")
    ):
        raise HTTPException(
            415,
            detail={"code": "invalid_image", "message": "An image body is required"},
        )
    filename = request.headers.get("x-filename", "image")
    if len(filename) > 255 or any(
        char in filename for char in ("/", "\\", "\x00", "\r", "\n")
    ):
        raise HTTPException(
            422,
            detail={
                "code": "invalid_filename",
                "message": "A plain filename is required",
            },
        )
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_IMAGE_BYTES:
            raise HTTPException(
                413,
                detail={
                    "code": "image_too_large",
                    "message": "Image exceeds the local upload limit",
                },
            )
        data.extend(chunk)
    return await _call(
        _service(request).upload_image(account_id, bytes(data), filename=filename)
    )


@router.post("/accounts/{account_id}/start-conversation", status_code=202)
async def start_conversation(
    request: Request, account_id: AccountPath, body: StartConversationRequest
) -> dict[str, Any]:
    return await _call(
        _service(request).submit_send(
            account_id,
            target_kind="listing",
            target_id=body.listing_id,
            text=body.text,
            attachment_ids=body.attachment_ids,
            idempotency_key=body.idempotency_key,
        )
    )


@router.post("/accounts/{account_id}/send-message", status_code=202)
async def send_message(
    request: Request, account_id: AccountPath, body: ReplyRequest
) -> dict[str, Any]:
    return await _call(
        _service(request).submit_send(
            account_id,
            target_kind="conversation",
            target_id=body.conversation_id,
            text=body.text,
            attachment_ids=body.attachment_ids,
            idempotency_key=body.idempotency_key,
        )
    )


@router.get("/accounts/{account_id}/operations/{operation_id}")
async def get_message_operation(
    request: Request, account_id: AccountPath, operation_id: ObjectPath
) -> dict[str, Any]:
    return await _call(_service(request).get_operation(account_id, operation_id))
