from datetime import datetime

from fastapi import APIRouter, Header, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from routers.jobs import (
    IDEMPOTENCY_KEY_PATTERN,
    JobAcceptedResponse,
    SearchJobRequest,
    submit_search_job,
)
from utils.url_security import (
    MAX_URL_LENGTH,
    UnsafeOutboundUrlError,
    validate_kleinanzeigen_url_syntax,
)

router = APIRouter()


class InserateByUrlRequest(BaseModel):
    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH)
    max_pages: int = Field(
        default=1,
        ge=1,
        deprecated=True,
        description=(
            "Ignored compatibility field; jobs discover pages automatically "
            "and release results in 200-item steps"
        ),
    )
    min_publish_date: datetime | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        try:
            return validate_kleinanzeigen_url_syntax(value)
        except UnsafeOutboundUrlError as exc:
            raise ValueError(str(exc)) from exc


@router.post(
    "/inserate-by-url",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAcceptedResponse,
    deprecated=True,
)
async def inserate_by_url(
    request: Request,
    response: Response,
    body: InserateByUrlRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=IDEMPOTENCY_KEY_PATTERN,
    ),
):
    """Accept the old URL body and enqueue the equivalent persistent job."""

    response.headers["Deprecation"] = "true"
    response.headers["Link"] = '</jobs/search>; rel="successor-version"'
    return await submit_search_job(
        request,
        SearchJobRequest(
            url=body.url,
            min_publish_date=body.min_publish_date,
        ),
        idempotency_key=idempotency_key,
    )
