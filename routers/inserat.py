"""Deprecated single-listing adapter for persistent detail jobs."""

from fastapi import APIRouter, Header, Path, Query, Request, Response, status

from routers.jobs import (
    IDEMPOTENCY_KEY_PATTERN,
    JobAcceptedResponse,
    submit_detail_job,
)
from utils.input_validation import (
    BATCH_ID_PATTERN,
    LISTING_ID_PATTERN,
    MAX_BATCH_ID_LENGTH,
    MAX_LISTING_ID_LENGTH,
)

router = APIRouter()


@router.get(
    "/inserat/{id}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAcceptedResponse,
    deprecated=True,
)
async def get_inserat(
    request: Request,
    response: Response,
    id: str = Path(
        ...,
        min_length=6,
        max_length=MAX_LISTING_ID_LENGTH,
        pattern=LISTING_ID_PATTERN,
        description="Numeric ad ID or ad-id-category-id-location-id segment",
    ),
    batch_id: str | None = Query(
        None,
        min_length=1,
        max_length=MAX_BATCH_ID_LENGTH,
        pattern=BATCH_ID_PATTERN,
        deprecated=True,
        description="Ignored legacy correlation ID",
    ),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=IDEMPOTENCY_KEY_PATTERN,
    ),
):
    """Enqueue one listing ID instead of scraping inside the HTTP request."""

    del batch_id
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = '</jobs/details>; rel="successor-version"'
    return await submit_detail_job(
        request,
        listing_ids=[id],
        idempotency_key=idempotency_key,
    )
