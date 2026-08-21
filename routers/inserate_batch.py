"""Deprecated batch adapter for persistent detail jobs."""

from fastapi import APIRouter, Header, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from routers.jobs import (
    IDEMPOTENCY_KEY_PATTERN,
    JobAcceptedResponse,
    submit_detail_job,
)
from utils.input_validation import normalize_listing_id

router = APIRouter()


class InserateBatchRequest(BaseModel):
    ids: list[str] = Field(min_length=1)
    max_concurrent: int | None = Field(
        default=None,
        deprecated=True,
        description="Ignored; global scheduling controls every navigation",
    )

    @field_validator("ids")
    @classmethod
    def validate_and_deduplicate_ids(cls, ids: list[str]) -> list[str]:
        """Validate IDs and preserve first-seen order without a batch cap."""

        unique_ids: list[str] = []
        seen: set[str] = set()
        for listing_id in ids:
            normalized = normalize_listing_id(listing_id)
            if normalized not in seen:
                seen.add(normalized)
                unique_ids.append(normalized)
        return unique_ids


@router.post(
    "/inserate/batch",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAcceptedResponse,
    deprecated=True,
)
async def get_inserate_batch(
    request: Request,
    response: Response,
    body: InserateBatchRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=IDEMPOTENCY_KEY_PATTERN,
    ),
):
    """Persist all requested IDs; the scheduler materializes them gradually."""

    response.headers["Deprecation"] = "true"
    response.headers["Link"] = '</jobs/details>; rel="successor-version"'
    return await submit_detail_job(
        request,
        listing_ids=body.ids,
        idempotency_key=idempotency_key,
    )
