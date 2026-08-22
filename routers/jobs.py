"""Persistente Job-API für kontrollierte Kleinanzeigen-Abrufe."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from utils.input_validation import normalize_listing_id
from utils.job_manager import JobManagerError
from utils.url_security import (
    MAX_URL_LENGTH,
    UnsafeOutboundUrlError,
    validate_kleinanzeigen_url_syntax,
)


router = APIRouter(prefix="/jobs", tags=["jobs"])

IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
JobType = Literal["search", "details"]
JobState = Literal[
    "queued",
    "running",
    "waiting_cooldown",
    "awaiting_continuation",
    "needs_attention",
    "completed",
    "cancelled",
]


class SearchJobRequest(BaseModel):
    """Either a structured search or a complete Kleinanzeigen search URL."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, min_length=1, max_length=MAX_URL_LENGTH)
    query: str | None = Field(default=None, max_length=512)
    location: str | None = Field(default=None, max_length=512)
    radius: int | None = Field(default=None, ge=0)
    min_price: int | None = Field(default=None, ge=0)
    max_price: int | None = Field(default=None, ge=0)
    min_publish_date: datetime | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return validate_kleinanzeigen_url_syntax(value)
        except UnsafeOutboundUrlError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def validate_search_variant(self) -> SearchJobRequest:
        if self.url is not None and any(
            value is not None
            for value in (
                self.query,
                self.location,
                self.radius,
                self.min_price,
                self.max_price,
            )
        ):
            raise ValueError("url cannot be combined with structured search filters")
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price must not exceed max_price")
        return self


class DetailJobRequest(BaseModel):
    """Create details from a search snapshot or an explicit listing-ID list."""

    model_config = ConfigDict(extra="forbid")

    source_search_job_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    listing_ids: list[str] | None = None

    @field_validator("listing_ids")
    @classmethod
    def validate_listing_ids(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        if not values:
            raise ValueError("listing_ids must not be empty")

        unique_ids: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = normalize_listing_id(value)
            if normalized not in seen:
                seen.add(normalized)
                unique_ids.append(normalized)
        return unique_ids

    @model_validator(mode="after")
    def validate_source(self) -> DetailJobRequest:
        has_source_job = self.source_search_job_id is not None
        has_listing_ids = self.listing_ids is not None
        if has_source_job == has_listing_ids:
            raise ValueError(
                "provide exactly one of source_search_job_id or listing_ids"
            )
        return self


class ContinueJobRequest(BaseModel):
    """Authorize exactly one 200-result continuation tranche."""

    model_config = ConfigDict(extra="forbid")

    continuation_token: str = Field(min_length=1, max_length=512)
    after_cursor: str | None = Field(default=None, min_length=1, max_length=512)


class ResumeJobRequest(BaseModel):
    """Retry exactly one persisted needs-attention cycle."""

    model_config = ConfigDict(extra="forbid")

    resume_token: str = Field(min_length=1, max_length=512)


class JobAcceptedResponse(BaseModel):
    """Stable submission/action response with discoverable follow-up URLs."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    type: JobType
    state: JobState
    idempotent_replay: bool = False
    status_url: str
    results_url: str


class JobStatusResponse(BaseModel):
    """Persisted status; nested metrics may evolve without breaking clients."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    type: JobType
    state: JobState
    progress: dict[str, Any] = Field(default_factory=dict)
    results: dict[str, Any] = Field(default_factory=dict)
    continuation: dict[str, Any] = Field(default_factory=dict)
    resume: dict[str, Any] = Field(default_factory=dict)
    cooldown: dict[str, Any] = Field(default_factory=dict)
    errors: dict[str, Any] = Field(default_factory=dict)


class JobResultsResponse(BaseModel):
    """Cursor-paginated results read exclusively from persistent storage."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    type: JobType
    items: list[dict[str, Any]]
    next_cursor: str | None = None
    has_more: bool


def get_job_manager(request: Request):
    """Return the process-local manager or a controlled readiness error."""

    manager = getattr(request.app.state, "job_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "job_manager_unavailable",
                "message": "Job manager is unavailable",
            },
        )
    return manager


def raise_job_manager_error(exc: JobManagerError) -> None:
    """Map storage/domain failures to non-sensitive HTTP errors."""

    headers: dict[str, str] = {}
    retry_after = getattr(exc, "retry_after_seconds", None)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    raise HTTPException(
        status_code=exc.http_status,
        headers=headers,
        detail={
            "error": exc.error_code,
            "message": str(exc),
        },
    ) from exc


def accepted_job(record: dict[str, Any]) -> dict[str, Any]:
    """Add stable relative URLs without discarding manager metadata."""

    job_id = str(record["job_id"])
    return {
        **record,
        "status_url": f"/jobs/{job_id}",
        "results_url": f"/jobs/{job_id}/results",
    }


async def submit_search_job(
    request: Request,
    search: SearchJobRequest,
    *,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Shared submission helper used by the canonical and legacy endpoints."""

    manager = get_job_manager(request)
    payload = search.model_dump(mode="json", exclude_none=True)
    try:
        record = await manager.create_search_job(
            payload,
            idempotency_key=idempotency_key,
        )
    except JobManagerError as exc:
        raise_job_manager_error(exc)
    return accepted_job(record)


async def submit_detail_job(
    request: Request,
    *,
    source_search_job_id: str | None = None,
    listing_ids: list[str] | None = None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Shared detail submission helper used by canonical and legacy endpoints."""

    manager = get_job_manager(request)
    try:
        record = await manager.create_detail_job(
            source_search_job_id=source_search_job_id,
            listing_ids=listing_ids,
            idempotency_key=idempotency_key,
        )
    except JobManagerError as exc:
        raise_job_manager_error(exc)
    return accepted_job(record)


@router.post(
    "/search",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAcceptedResponse,
)
async def create_search_job(
    request: Request,
    body: SearchJobRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=IDEMPOTENCY_KEY_PATTERN,
    ),
):
    return await submit_search_job(
        request,
        body,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/details",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAcceptedResponse,
)
async def create_detail_job(
    request: Request,
    body: DetailJobRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=IDEMPOTENCY_KEY_PATTERN,
    ),
):
    return await submit_detail_job(
        request,
        source_search_job_id=body.source_search_job_id,
        listing_ids=body.listing_ids,
        idempotency_key=idempotency_key,
    )


@router.get("/{job_id}", response_model=JobStatusResponse)
async def get_job(
    request: Request,
    job_id: str,
):
    manager = get_job_manager(request)
    try:
        return await manager.get_job(job_id)
    except JobManagerError as exc:
        raise_job_manager_error(exc)


@router.get("/{job_id}/results", response_model=JobResultsResponse)
async def get_job_results(
    request: Request,
    job_id: str,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=50, ge=1, le=200),
):
    manager = get_job_manager(request)
    try:
        return await manager.get_results(job_id, cursor=cursor, limit=limit)
    except JobManagerError as exc:
        raise_job_manager_error(exc)


@router.post(
    "/{job_id}/continue",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAcceptedResponse,
)
async def continue_job(
    request: Request,
    job_id: str,
    body: ContinueJobRequest,
):
    manager = get_job_manager(request)
    try:
        record = await manager.continue_job(
            job_id,
            continuation_token=body.continuation_token,
            after_cursor=body.after_cursor,
        )
    except JobManagerError as exc:
        raise_job_manager_error(exc)
    return accepted_job(record)


@router.post(
    "/{job_id}/resume",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAcceptedResponse,
)
async def resume_job(request: Request, job_id: str, body: ResumeJobRequest):
    manager = get_job_manager(request)
    try:
        record = await manager.resume_job(job_id, resume_token=body.resume_token)
    except JobManagerError as exc:
        raise_job_manager_error(exc)
    return accepted_job(record)


@router.delete("/{job_id}", response_model=JobAcceptedResponse)
async def cancel_job(request: Request, job_id: str):
    manager = get_job_manager(request)
    try:
        record = await manager.cancel_job(job_id)
    except JobManagerError as exc:
        raise_job_manager_error(exc)
    return accepted_job(record)
