"""Deprecated GET adapter for the persistent search-job API."""

from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status

from routers.jobs import (
    IDEMPOTENCY_KEY_PATTERN,
    JobAcceptedResponse,
    SearchJobRequest,
    submit_search_job,
)

router = APIRouter()


@router.get(
    "/inserate",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAcceptedResponse,
    deprecated=True,
)
async def get_inserate_ultra_optimized(
    request: Request,
    response: Response,
    query: str | None = Query(None, max_length=512, description="Search query"),
    location: str | None = Query(None, max_length=512, description="Location"),
    radius: int | None = Query(None, ge=0, description="Search radius in km"),
    min_price: int | None = Query(None, ge=0, description="Minimum price"),
    max_price: int | None = Query(None, ge=0, description="Maximum price"),
    page_count: int = Query(
        1,
        ge=1,
        deprecated=True,
        description=(
            "Ignored compatibility parameter; persistent jobs discover pages "
            "automatically and pause after each 200 released results"
        ),
    ),
    min_publish_date: datetime | None = Query(
        None,
        description="Stop once older listings are reached",
    ),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=IDEMPOTENCY_KEY_PATTERN,
    ),
):
    """Accept the former search shape and enqueue a persistent search job."""

    if min_price is not None and max_price is not None and min_price > max_price:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="min_price must not exceed max_price",
        )

    # Keep this compatibility parameter without allowing it to truncate a job.
    del page_count
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = '</jobs/search>; rel="successor-version"'
    return await submit_search_job(
        request,
        SearchJobRequest(
            query=query,
            location=location,
            radius=radius,
            min_price=min_price,
            max_price=max_price,
            min_publish_date=min_publish_date,
        ),
        idempotency_key=idempotency_key,
    )
